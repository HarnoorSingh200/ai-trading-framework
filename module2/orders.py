"""Handing a decision to Module 3, without making Module 3 Module 2's problem.

A decision is recorded first and forwarded second, and the forward is
best-effort on purpose. Module 3 may not be running, and a cycle that produced a
good decision must not be failed by a module that is not there to receive it.
What was recorded stays recorded; Module 3 reconciles against Module 2's own
log, so an order taken while it was down is still picked up rather than lost,
and the failure is written into the activity feed where an operator can see it.

It holds no credential and needs none: Module 3 is a loopback service that
tracks positions, and it cannot place an order either.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

#: Short, because a decision must never be held up by its own paperwork.
DEFAULT_DELIVER_TIMEOUT_SECONDS: float = 10.0


@runtime_checkable
class DecisionSink(Protocol):
    """Somewhere a recorded decision can be handed on to."""

    def deliver(self, decision: Mapping[str, Any]) -> bool: ...


class OrderBookError(RuntimeError):
    """Module 3 could not be read. The message never contains a credential."""


@dataclass(slots=True)
class OrderForwarder:
    """Posts a decision to Module 3's order book."""

    base_url: str
    client: httpx.Client | None = None
    timeout_seconds: float = DEFAULT_DELIVER_TIMEOUT_SECONDS
    calls: int = 0
    delivered: int = 0
    failures: int = 0
    last_error: str | None = None
    _owns_client: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(
                base_url=self.base_url.rstrip("/"), timeout=self.timeout_seconds
            )
            self._owns_client = True

    def deliver(self, decision: Mapping[str, Any]) -> bool:
        """Hand one decision over. ``False`` means it did not arrive."""

        client = self.client
        if client is None:  # pragma: no cover - built in __post_init__
            return False
        self.calls += 1
        try:
            response = client.post("/v1/orders", json=dict(decision))
        except httpx.HTTPError as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return False
        if response.status_code >= 400:
            self.failures += 1
            self.last_error = (
                f"HTTP {response.status_code}: {_refusal(response)}"[:200]
            )
            return False
        self.delivered += 1
        self.last_error = None
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url.rstrip("/"),
            "calls": self.calls,
            "delivered": self.delivered,
            "failures": self.failures,
            "last_error": self.last_error,
            "note": (
                "a decision is recorded before it is handed on, and Module 3 "
                "reconciles against this module's log, so a failed hand-over "
                "delays an order rather than losing it"
            ),
        }

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()


def _refusal(response: httpx.Response) -> str:
    """Module 3's own words about why it would not take the decision."""

    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
            return str(detail["error"].get("message") or "")[:200]
        if isinstance(payload.get("error"), dict):
            return str(payload["error"].get("message") or "")[:200]
    return (response.text or "")[:200]


@dataclass(slots=True)
class OrderReader:
    """Reads an agent's own orders back out of Module 3.

    An agent that cannot see what it already holds will happily open the same
    trade twice, and nothing downstream stops it. So this is the one piece of
    its own history an agent is given: the orders it has placed, open and
    finished.

    *Its own* is the whole point. The request names the agent that asked and
    Module 3 filters on it, so a model can never be shown another agent's
    positions - not by asking nicely, and not by asking for a different id.
    """

    base_url: str
    client: httpx.Client | None = None
    timeout_seconds: float = DEFAULT_DELIVER_TIMEOUT_SECONDS
    calls: int = 0
    failures: int = 0
    last_error: str | None = None
    _owns_client: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(
                base_url=self.base_url.rstrip("/"), timeout=self.timeout_seconds
            )
            self._owns_client = True

    def mine(
        self, agent_id: str, *, status: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """One agent's orders, newest first, or a refusal that says why."""

        client = self.client
        if client is None:  # pragma: no cover - built in __post_init__
            raise OrderBookError("no HTTP client is configured")
        params: dict[str, Any] = {"agent_id": agent_id, "limit": int(limit)}
        if status:
            params["status"] = status
        self.calls += 1
        try:
            response = client.get("/v1/orders", params=params)
        except httpx.HTTPError as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            raise OrderBookError(
                f"the order book is unreachable at {self.base_url}: "
                f"{type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            self.failures += 1
            message = _refusal(response)
            self.last_error = f"HTTP {response.status_code}: {message}"[:200]
            raise OrderBookError(f"the order book refused the read: {message}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OrderBookError("the order book returned a non-JSON answer") from exc
        self.last_error = None
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    def summary(self, agent_id: str, *, day: str | None = None) -> dict[str, Any]:
        """How one agent's book is doing: what is open, and what it has made.

        Module 3 keeps a book per exchange day, so this is the day's answer by
        default - "how am I doing right now" - and it carries the open positions
        themselves, which is the half an agent needs before it decides to open
        another one.
        """

        client = self.client
        if client is None:  # pragma: no cover - built in __post_init__
            raise OrderBookError("no HTTP client is configured")
        params: dict[str, Any] = {"agent_id": agent_id}
        if day:
            params["day"] = day
        self.calls += 1
        try:
            response = client.get("/v1/orders/summary", params=params)
        except httpx.HTTPError as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            raise OrderBookError(
                f"the order book is unreachable at {self.base_url}: "
                f"{type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            self.failures += 1
            message = _refusal(response)
            self.last_error = f"HTTP {response.status_code}: {message}"[:200]
            raise OrderBookError(f"the order book refused the read: {message}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OrderBookError("the order book returned a non-JSON answer") from exc
        self.last_error = None
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    def describe(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url.rstrip("/"),
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

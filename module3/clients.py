"""The two neighbours Module 3 depends on, over HTTP and nothing else.

Prices come from Module 1, and the decisions this module reconciles against
come from Module 2. Neither is read from its storage: a module that reads
another module's files has one interface for reading it and none for changing
it, and the first refactor of that storage breaks a module that was never told.

The event-stream parsing below is a deliberate copy of Module 2's rather than a
shared package. These are separate processes with separate lives, and the
alternative - a fifth package that exists only to hold forty lines both of them
already have - buys coupling and loses nothing worth keeping.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

import httpx

from module3.timeutil import utc_now

#: How long to wait between reconnects of the price stream, and the ceiling.
BACKOFF_START_SECONDS: float = 2.0
BACKOFF_MAX_SECONDS: float = 60.0


class MarketError(RuntimeError):
    """A neighbour could not answer, or answered with a refusal."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Turn server-sent event lines into ``{"event", "data"}`` dictionaries.

    Comments (``: keepalive``) are ignored, and a data payload that will not
    parse is dropped: a torn frame is not worth ending a stream over.
    """

    name: str | None = None
    data: list[str] = []
    for line in lines:
        text = line.rstrip("\r")
        if not text:
            if name and data:
                try:
                    payload = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    yield {"event": name, "data": payload}
            name, data = None, []
            continue
        if text.startswith(":"):
            continue
        field, _, value = text.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)


def unwrap(response: httpx.Response) -> dict[str, Any]:
    """Module 1's and Module 2's ``{ok, data}`` envelope, or its refusal.

    A refusal keeps the words of the module that produced it: when Module 1 says
    a symbol is not one it covers, that sentence is the whole answer, and
    rewriting it here would lose the detail the operator needs.
    """

    try:
        payload = response.json()
    except ValueError as exc:
        raise MarketError(
            f"a neighbour returned a non-JSON answer for {response.request.url.path}",
            status_code=response.status_code,
        ) from exc
    body = payload if isinstance(payload, dict) else {}
    detail = body.get("detail")
    error = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(error, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else None
    if response.status_code >= 400 or error is not None or body.get("ok") is False:
        details = error or {}
        raise MarketError(
            str(details.get("message") or f"HTTP {response.status_code}"),
            code=str(details.get("code") or "") or None,
            status_code=response.status_code,
        )
    data = body.get("data")
    return data if isinstance(data, dict) else body


class MarketClient:
    """The one read Module 3 needs from Module 1: what a stock costs now."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], Any] = utc_now,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout_seconds
        )
        self._clock = clock
        self.calls = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def prices(self, symbols: Sequence[str]) -> dict[str, Any]:
        """The latest price of named stocks, with its source and its age."""

        wanted = ",".join(str(symbol) for symbol in symbols)
        try:
            response = self._client.get(
                "/v1/market/prices", params={"symbols": wanted}
            )
        except httpx.HTTPError as exc:
            raise MarketError(
                f"Module 1 is unreachable at {self._base_url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self.calls += 1
        return unwrap(response)


class PriceStream:
    """A reconnecting subscription to the prices of a changing set of stocks.

    The set is read from ``symbols`` rather than fixed at construction, because
    what Module 3 holds changes as orders arrive and finish. The subscription is
    per-connection on the wire, so when the set changes the stream is dropped
    and opened again - and the first frame of every connection carries every
    watched stock, which is what stops a stock that never trades from leaving an
    order pending for ever.
    """

    def __init__(
        self,
        base_url: str,
        *,
        symbols: Callable[[], Sequence[str]],
        on_prices: Callable[[list[dict[str, Any]]], None],
        client: httpx.Client | None = None,
        clock: Callable[[], Any] = utc_now,
        backoff_seconds: float = BACKOFF_START_SECONDS,
        max_backoff_seconds: float = BACKOFF_MAX_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self._base_url, timeout=30.0)
        self._symbols = symbols
        self._on_prices = on_prices
        self._clock = clock
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._sleep = sleeper
        self._lock = threading.RLock()
        self._state = "idle"
        self._connected = False
        self._watching = 0
        self._frames = 0
        self._prices = 0
        self._reconnects = 0
        self._last_message_at: Any = None
        self._last_error: str | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "connected": self._connected,
                "watching": self._watching,
                "frames": self._frames,
                "prices": self._prices,
                "reconnects": self._reconnects,
                "last_message_at": (
                    None if self._last_message_at is None else self._last_message_at.isoformat()
                ),
                "last_error": self._last_error,
            }

    def run(self, stop: threading.Event) -> None:
        """Subscribe until told to stop, reconnecting around any failure."""

        attempts = 0
        while not stop.is_set():
            wanted = self._wanted()
            if not wanted:
                # Nothing is held, so there is nothing to watch. Waiting is not
                # idling: an idle subscription would spend a slot on Module 1's
                # stream to be told about stocks nobody has a position in.
                with self._lock:
                    self._state = "idle"
                    self._connected = False
                    self._watching = 0
                stop.wait(1.0)
                continue
            try:
                self._connect_once(wanted)
                attempts = 0
            except MarketError as exc:
                with self._lock:
                    self._last_error = str(exc)[:200]
            except Exception as exc:  # noqa: BLE001 - the loop outlives any one socket
                if stop.is_set():
                    break
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if stop.is_set():
                break
            attempts += 1
            with self._lock:
                self._connected = False
                self._reconnects += 1
                self._state = f"reconnecting:{attempts}"
            stop.wait(min(self._backoff * attempts, self._max_backoff))
        with self._lock:
            self._connected = False
            self._state = "stopped"

    # -- internals ----------------------------------------------------------- #

    def _wanted(self) -> tuple[str, ...]:
        return tuple(sorted({str(symbol).strip().upper() for symbol in self._symbols() if str(symbol).strip()}))

    def _connect_once(self, wanted: tuple[str, ...]) -> None:
        params = {"symbols": ",".join(wanted)}
        with self._client.stream(
            "GET", "/v1/market/prices/stream", params=params, timeout=None
        ) as response:
            if not response.is_success:
                raise MarketError(
                    f"the price stream answered HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            with self._lock:
                self._state = "connected"
                self._connected = True
                self._watching = len(wanted)
                self._last_error = None
            for event in parse_sse(response.iter_lines()):
                if event.get("event") != "prices":
                    continue
                payload = event.get("data")
                rows = payload.get("rows") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    continue
                with self._lock:
                    self._frames += 1
                    self._prices += len(rows)
                    self._last_message_at = self._clock()
                self._on_prices(rows)
                if self._wanted() != wanted:
                    # What is held has changed, and the subscription is part of
                    # the request: the only way to change it is to ask again.
                    return
        with self._lock:
            self._connected = False


class DecisionsClient:
    """Module 2's recorded decisions, for what a push may have missed."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout_seconds
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def agents(self) -> list[str]:
        """Every agent Module 2 currently lists."""

        payload = self._get("/v1/agents")
        found = payload.get("agents")
        if not isinstance(found, list):
            return []
        return [
            str(agent.get("agent_id"))
            for agent in found
            if isinstance(agent, dict) and agent.get("agent_id")
        ]

    def decisions(self, agent_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent decisions one agent recorded."""

        payload = self._get(
            f"/v1/agents/{agent_id}/decisions", params={"limit": int(limit)}
        )
        found = payload.get("decisions")
        if not isinstance(found, list):
            return []
        return [item for item in found if isinstance(item, dict)]

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise MarketError(
                f"Module 2 is unreachable at {self._base_url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return unwrap(response)

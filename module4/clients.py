"""The order book and the agent engine's usage, read through their own APIs.

Nothing is read from Module 3's storage. A module that reads another module's
files has one interface for reading it and none for changing it, and the first
refactor of that storage breaks a module nobody told.

The book is read *by day*, which is how Module 3 keeps it and how this module
wants it: a day is the unit that stops changing.
"""

from __future__ import annotations

from typing import Any

import httpx


class BookError(RuntimeError):
    """Module 3 could not be read."""

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


class OrderBookClient:
    """The two reads this module needs: which days exist, and one day's orders."""

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
        self.calls = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def days(self) -> list[str]:
        """Every exchange day Module 3 still holds a book for, newest first."""

        payload = self._get("/v1/orders/days")
        found = payload.get("days")
        if not isinstance(found, list):
            return []
        return [
            str(row.get("day"))
            for row in found
            if isinstance(row, dict) and row.get("day")
        ]

    def for_day(self, day: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        """One day's orders, open and finished."""

        payload = self._get("/v1/orders", params={"day": day, "limit": int(limit)})
        found = payload.get("orders")
        if not isinstance(found, list):
            return []
        return [row for row in found if isinstance(row, dict)]

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise BookError(
                f"Module 3 is unreachable at {self._base_url}: {type(exc).__name__}"
            ) from exc
        self.calls += 1
        try:
            payload = response.json()
        except ValueError as exc:
            raise BookError(
                f"Module 3 returned a non-JSON answer for {path}",
                status_code=response.status_code,
            ) from exc
        body = payload if isinstance(payload, dict) else {}
        detail = body.get("detail")
        error = detail.get("error") if isinstance(detail, dict) else None
        if not isinstance(error, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else None
        if response.status_code >= 400 or error is not None or body.get("ok") is False:
            details = error or {}
            raise BookError(
                str(details.get("message") or f"HTTP {response.status_code}"),
                code=str(details.get("code") or "") or None,
                status_code=response.status_code,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else body


class UsageError(RuntimeError):
    """Module 2 could not be read."""

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


class AgentUsageClient:
    """The one read this module needs from Module 2: a day's token totals.

    The activity logs belong to Module 2, so the totals are asked for through
    its API; this module never opens a file in another module's directory.
    """

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
        self.calls = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def usage(self, day: str) -> dict[str, Any]:
        """One exchange day's totals, per agent and overall."""

        payload = self._get("/v1/usage", params={"day": day})
        return payload if isinstance(payload, dict) else {}

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise UsageError(
                f"Module 2 is unreachable at {self._base_url}: {type(exc).__name__}"
            ) from exc
        self.calls += 1
        try:
            payload = response.json()
        except ValueError as exc:
            raise UsageError(
                f"Module 2 returned a non-JSON answer for {path}",
                status_code=response.status_code,
            ) from exc
        body = payload if isinstance(payload, dict) else {}
        detail = body.get("detail")
        error = detail.get("error") if isinstance(detail, dict) else None
        if not isinstance(error, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else None
        if response.status_code >= 400 or error is not None or body.get("ok") is False:
            details = error or {}
            raise UsageError(
                str(details.get("message") or f"HTTP {response.status_code}"),
                code=str(details.get("code") or "") or None,
                status_code=response.status_code,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else body


class BenchmarkError(RuntimeError):
    """Module 1's benchmark route could not be read."""

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


class BenchmarkClient:
    """The one read this module needs from Module 1: the benchmark index.

    The index belongs to Module 1 because market data does; this module asks for
    the daily closes over HTTP and never invents a market number of its own.
    """

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
        self.calls = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def benchmark(self) -> dict[str, Any]:
        """The benchmark's daily closes, oldest first."""

        payload = self._get("/v1/market/benchmark")
        return payload if isinstance(payload, dict) else {}

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise BenchmarkError(
                f"Module 1 is unreachable at {self._base_url}: {type(exc).__name__}"
            ) from exc
        self.calls += 1
        try:
            payload = response.json()
        except ValueError as exc:
            raise BenchmarkError(
                f"Module 1 returned a non-JSON answer for {path}",
                status_code=response.status_code,
            ) from exc
        body = payload if isinstance(payload, dict) else {}
        detail = body.get("detail")
        error = detail.get("error") if isinstance(detail, dict) else None
        if not isinstance(error, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else None
        if response.status_code >= 400 or error is not None or body.get("ok") is False:
            details = error or {}
            raise BenchmarkError(
                str(details.get("message") or f"HTTP {response.status_code}"),
                code=str(details.get("code") or "") or None,
                status_code=response.status_code,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else body


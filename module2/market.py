"""Module 1's HTTP surface, as Module 2 uses it.

Every market fact this module needs comes from here: which candle has closed,
what the universe looks like on one timeframe, what one stock's finished candle
and levels are. Nothing is read from Module 1's storage, and nothing is assumed
about its internals - the endpoints are the whole contract.

A refusal from Module 1 keeps Module 1's own words. When a screen is rejected
because a field does not exist, that sentence is exactly what the model needs
to correct itself, and rewriting it here would lose the detail.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from module2.timeutil import utc_now


class MarketError(RuntimeError):
    """Module 1 could not answer, or answered with a refusal."""

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


class MarketClient:
    """The reads Module 2 builds its tools on: one market, over HTTP."""

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

    # -- the simple reads ---------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        return self._get("/v1/market/status")

    def timeframes(self) -> dict[str, Any]:
        return self._get("/v1/market/timeframes")

    def session_clock(self) -> dict[str, Any]:
        return self._get("/v1/market/session-clock")

    def universe(self) -> dict[str, Any]:
        return self._get("/v1/market/universe")

    def fields(self, interval: str) -> dict[str, Any]:
        return self._get("/v1/market/fields", params={"interval": interval})

    def screen(self, **body: Any) -> dict[str, Any]:
        return self._post("/v1/market/screen", body)

    def stock(self, symbol: str, interval: str) -> dict[str, Any]:
        return self._get(
            f"/v1/market/symbol/{symbol}", params={"interval": interval}
        )

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        return self._get(
            "/v1/market/candles",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    def market_state(self) -> dict[str, Any]:
        """Everything a model needs to know about whether now is a sane moment."""

        status = self.status()
        board = status.get("board") or {}
        return {
            "session": board.get("session_clock") or {},
            "feed": board.get("feed") or {},
            "board": board.get("board") or {},
            "candles": (status.get("candles") or {}).get("book") or {},
            "universe": status.get("universe") or {},
        }

    def bar_status(self, interval: str) -> dict[str, Any]:
        """The candle that has just closed, and whether it is finished being built.

        The agent wakes on its own clock at the boundary, which is a moment
        before this answer exists: a minute is only closed into the book when
        the first tick of the next one arrives. So it asks this and waits for a
        fact, rather than sleeping a number of seconds somebody guessed.
        """

        return self._get("/v1/market/bar-status", params={"interval": interval})

    # -- transport ----------------------------------------------------------- #

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise MarketError(
                f"Module 1 is unreachable at {self._base_url}: {type(exc).__name__}: {exc}"
            ) from exc
        return self._unwrap(response)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise MarketError(
                f"Module 1 is unreachable at {self._base_url}: {type(exc).__name__}: {exc}"
            ) from exc
        return self._unwrap(response)

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        self.calls += 1
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketError(
                f"Module 1 returned a non-JSON answer for {response.request.url.path}",
                status_code=response.status_code,
            ) from exc
        body = payload if isinstance(payload, dict) else {}
        error = body.get("detail", {}).get("error") if isinstance(body.get("detail"), dict) else None
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

"""The index the agents are measured against, fetched from the same broker.

Module 1 owns every market read, and a benchmark is a market fact like any
other: this is not a second data source, it is one more instrument on the
session that already exists. Indices are not in the tradable universe, so the
lookup is the one place that asks the instrument dump for an INDICES row.

The answer is daily closes and it is cached for a while: a daily candle changes
once a day at most, and the dashboard polls this route far more often than the
broker needs to be asked.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

from module1.broker.kite import KiteClient, KiteError
from module1.instruments import InstrumentError, InstrumentMaster
from module1.timeutil import format_utc, to_ist, utc_now

DEFAULT_SYMBOL: Final[str] = "NIFTY 50"
DEFAULT_DAYS: Final[int] = 120
DEFAULT_REFRESH_SECONDS: Final[float] = 900.0


class BenchmarkError(RuntimeError):
    """The benchmark index could not be read from the broker."""


class BenchmarkService:
    """Daily closes for one index, cached between reads."""

    def __init__(
        self,
        *,
        master: InstrumentMaster,
        client: KiteClient,
        symbol: str = DEFAULT_SYMBOL,
        days: int = DEFAULT_DAYS,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._master = master
        self._client = client
        self._symbol = symbol
        self._days = max(1, int(days))
        self._refresh = max(1.0, float(refresh_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._candles: list[dict[str, Any]] = []
        self._fetched_at: datetime | None = None
        self._token: str | None = None
        self._last_error: str | None = None
        self.fetches = 0

    @property
    def symbol(self) -> str:
        return self._symbol

    def daily(self) -> dict[str, Any]:
        """The index's daily closes, oldest first, fetched no more than needed."""

        with self._lock:
            if self._is_fresh():
                return self._payload()
        try:
            record = self._master.index_record(self._symbol)
            end = self._clock()
            start = end - timedelta(days=self._days)
            candles = self._client.historical(
                str(record.instrument_token), "1d", start=start, end=end
            )
        except (InstrumentError, KiteError) as exc:
            with self._lock:
                self._last_error = str(exc)[:200]
            raise BenchmarkError(str(exc)) from exc

        closes: dict[str, float] = {}
        for candle in candles:
            day = to_ist(candle.open_time).date().isoformat()
            closes[day] = float(candle.close)
        rows = [
            {"date": day, "close": closes[day]} for day in sorted(closes)
        ]
        with self._lock:
            self._candles = rows
            self._fetched_at = self._clock()
            self._token = str(record.instrument_token)
            self._last_error = None
            self.fetches += 1
            return self._payload()

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "symbol": self._symbol,
                "days": self._days,
                "candles": len(self._candles),
                "fetched_at": (
                    None if self._fetched_at is None else format_utc(self._fetched_at)
                ),
                "fetches": self.fetches,
                "last_error": self._last_error,
            }

    def _is_fresh(self) -> bool:
        if not self._candles or self._fetched_at is None:
            return False
        age = (self._clock() - self._fetched_at).total_seconds()
        return age < self._refresh

    def _payload(self) -> dict[str, Any]:
        return {
            "symbol": self._symbol,
            "instrument_token": self._token,
            "interval": "1d",
            "source": "broker historical (1d)",
            "fetched_at": (
                None if self._fetched_at is None else format_utc(self._fetched_at)
            ),
            "candles": list(self._candles),
        }

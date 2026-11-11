"""The broker HTTP client Module 1 reads candles through.

Only the routes a web session can actually reach live here. Kite's website API
serves historical candles, margins, orders and positions; it has no quote
route at all, which is why live prices arrive over the websocket in
:mod:`module1.broker.feed` instead of over HTTP.

Two behaviours in this client are deliberate:

* **Paging.** Kite answers a minute request with at most 360 candles - six
  hours, fifteen minutes short of a full NSE session - so the end of a session
  is silently missing from a single request. The client asks again from where
  the last answer stopped.
* **Self-healing.** A refusal from the broker is treated as an expired session
  rather than as a fatal error: the session is repaired and the request is
  retried once.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from module1.broker.session import (
    KITE_VERSION_HEADER,
    KITE_WEB_BASE_URL,
    SessionManager,
)
from module1.limiter import RateLimiter, unlimited
from module1.timeutil import IST, parse_timestamp, to_ist, to_utc, utc_now

#: Kite returns at most this many candles per calendar day in one request.
MAX_CANDLES_PER_DAY: Final[int] = 360

#: How many follow-up requests one truncated day may take. One is normally
#: enough; the bound exists so a broker that stops advancing cannot loop.
MAX_TAIL_PAGES: Final[int] = 6

#: How long a "this day had no tail" verdict is trusted. The tail is a property
#: of the broker's data window rather than of one instrument, so one probe per
#: day is normally enough - and without remembering it, every symbol in the
#: universe pays for the same answer. It is re-checked periodically so a broker
#: that starts publishing more of the session is noticed.
TAIL_VERDICT_TTL_SECONDS: Final[float] = 600.0

#: Routes, relative to the website host.
HISTORICAL_PATH: Final[str] = "/oms/instruments/historical/{token}/{interval}"
PROFILE_PATH: Final[str] = "/oms/user/profile"

#: Interval names Kite accepts, and how many minutes each bucket covers.
INTERVAL_PARAMETER: Final[dict[str, str]] = {
    "1m": "minute",
    "3m": "3minute",
    "5m": "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "60m": "60minute",
    "1d": "day",
}

INTERVAL_MINUTES: Final[dict[str, int]] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "1d": 1440,
}


class KiteError(RuntimeError):
    """A broker call failed. The message never contains a credential."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Candle:
    """One candle from the broker. Prices are exact decimals."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    #: "broker" for a historical record, "live" for one folded together here
    #: from the tick stream.
    source: str = "broker"

    def describe(self) -> dict[str, Any]:
        return {
            "open_time": self.open_time.isoformat(),
            "open": format(self.open, "f"),
            "high": format(self.high, "f"),
            "low": format(self.low, "f"),
            "close": format(self.close, "f"),
            "volume": format(self.volume, "f"),
            "source": self.source,
        }


class KiteClient:
    """Talk to Kite's website API with a web session."""

    def __init__(
        self,
        session: SessionManager,
        *,
        base_url: str = KITE_WEB_BASE_URL,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
        rate_limit_seconds: float = 0.35,
        limiter: RateLimiter | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout_seconds, follow_redirects=True
        )
        self._rate_limit = max(0.0, float(rate_limit_seconds))
        if limiter is not None:
            self._limiter = limiter
        elif self._rate_limit > 0:
            # One shared budget for the account, not per connection.
            self._limiter = RateLimiter(rate_per_second=1.0 / self._rate_limit)
        else:
            self._limiter = unlimited()
        self._clock = clock
        self._sleep = sleeper
        self.calls = 0
        self.refusals = 0
        self._tail_verdict: dict[date, tuple[float, bool]] = {}
        self._verdict_lock = threading.Lock()
        self.tail_probes_skipped = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    def status(self) -> dict[str, Any]:
        return {
            "base_url": self._base_url,
            "calls": self.calls,
            "refusals_repaired": self.refusals,
            "rate_limit_seconds": self._rate_limit,
            "limiter": self._limiter.describe(),
            "tail_probes_skipped": self.tail_probes_skipped,
        }

    # -- routes ------------------------------------------------------------- #

    def profile(self) -> dict[str, Any]:
        """The signed-in user's profile, the cheapest proof of a live session."""

        return self._json("GET", PROFILE_PATH)

    def historical(
        self,
        instrument_token: str,
        interval: str = "1m",
        *,
        start: datetime,
        end: datetime,
        interactive: bool = False,
    ) -> list[Candle]:
        """Every candle between two instants, past Kite's per-day cap.

        Kite returns at most 360 candles for any one calendar day, and it takes
        the *first* 360 of the range it was given. On a normal NSE session of
        375 minutes that is fifteen minutes short of the close - but because
        the bound is a whole day, the obvious follow-up ("ask again from the
        last candle") sends the same date and gets the same answer back.
        The follow-up therefore has to name a time of day, which is what
        ``_history_page`` now always does.
        """

        if interval not in INTERVAL_PARAMETER:
            raise KiteError(
                f"unsupported interval {interval!r}", error_type="InputException"
            )
        step = timedelta(minutes=INTERVAL_MINUTES[interval])
        deadline = to_utc(end)
        first = self._history_page(
            instrument_token, interval, to_utc(start), deadline, interactive=interactive
        )
        merged: dict[datetime, Candle] = {candle.open_time: candle for candle in first}

        for day in _days_at_the_cap(merged.values()):
            if not self._should_probe_tail(day):
                self.tail_probes_skipped += 1
                continue
            cursor = max(
                candle.open_time for candle in merged.values() if _ist_day(candle) == day
            ) + step
            day_end = datetime.combine(day + timedelta(days=1), clock_time(0, 0), tzinfo=IST)
            window_end = min(day_end - timedelta(seconds=1), deadline)
            found = False
            for _page in range(MAX_TAIL_PAGES):
                if cursor > window_end:
                    break
                page = self._history_page(
                    instrument_token, interval, cursor, window_end, interactive=interactive
                )
                fresh = [c for c in page if c.open_time not in merged]
                if not fresh:
                    break
                found = True
                for candle in fresh:
                    merged[candle.open_time] = candle
                cursor = max(candle.open_time for candle in fresh) + step
                if len(page) < MAX_CANDLES_PER_DAY:
                    break
            self._record_tail_verdict(day, had_tail=found)

        return sorted(merged.values(), key=lambda candle: candle.open_time)

    # -- the per-day tail probe --------------------------------------------- #

    def _should_probe_tail(self, day: date) -> bool:
        """Whether asking for the rest of ``day`` is worth a request.

        A day that hit the candle cap *might* have more after it. On this
        broker it never does - the cash segment's minute window ends at 15:14,
        which is exactly the cap - so the answer is remembered per day and the
        other forty-nine symbols skip the question. A day that *did* have more
        is always asked about again.
        """

        with self._verdict_lock:
            entry = self._tail_verdict.get(day)
        if entry is None:
            return True
        checked_at, had_tail = entry
        if had_tail:
            return True
        return (time.monotonic() - checked_at) > TAIL_VERDICT_TTL_SECONDS

    def _record_tail_verdict(self, day: date, *, had_tail: bool) -> None:
        with self._verdict_lock:
            self._tail_verdict[day] = (time.monotonic(), had_tail)

    def day_candles(
        self,
        instrument_token: str,
        *,
        sessions: int = 10,
        now: datetime | None = None,
        interactive: bool = False,
    ) -> list[Candle]:
        """Daily candles covering roughly the last ``sessions`` sessions."""

        moment = to_utc(now or self._clock())
        start = moment - timedelta(days=max(2, int(sessions)) + 5)
        return self.historical(
            instrument_token, "1d", start=start, end=moment, interactive=interactive
        )

    def _history_page(
        self,
        instrument_token: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        interactive: bool = False,
    ) -> list[Candle]:
        data = self._json(
            "GET",
            HISTORICAL_PATH.format(
                token=str(instrument_token), interval=INTERVAL_PARAMETER[interval]
            ),
            params={
                # Kite accepts a date or a timestamp here, and only a timestamp
                # can say "the rest of this same day". It rejects ISO's "T".
                # The exchange reads these as IST, so they are converted:
                # sending 03:45 UTC where 09:15 IST was meant would ask for a
                # six-hour-early window and quietly return the wrong candles.
                "from": to_ist(start).strftime("%Y-%m-%d %H:%M:%S"),
                "to": to_ist(end).strftime("%Y-%m-%d %H:%M:%S"),
            },
            interactive=interactive,
        )
        rows = data.get("candles") if isinstance(data, dict) else None
        if not rows:
            return []
        candles: list[Candle] = []
        for row in rows:
            candle = _candle(row)
            if candle is not None:
                candles.append(candle)
        return candles

    # -- transport ---------------------------------------------------------- #

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        interactive: bool = False,
    ) -> dict[str, Any]:
        response = self._send(method, path, params=params, interactive=interactive)
        try:
            payload = response.json()
        except ValueError as exc:
            raise KiteError(
                f"the broker returned a non-JSON response for {path}",
                status_code=response.status_code,
            ) from exc
        body = payload if isinstance(payload, dict) else {}
        if response.status_code >= 400 or str(body.get("status") or "").lower() == "error":
            raise KiteError(
                f"the broker rejected {path}: "
                f"{str(body.get('message') or response.status_code)[:200]}",
                status_code=response.status_code,
                error_type=str(body.get("error_type") or "") or None,
                retryable=response.status_code >= 500,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {"data": data}

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        interactive: bool = False,
    ) -> httpx.Response:
        """One request, self-healing a refused session exactly once."""

        for attempt in (1, 2):
            self._limiter.acquire(interactive=interactive)
            try:
                response = self._client.request(
                    method, path, params=params, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                raise KiteError(
                    f"the broker is unreachable: {type(exc).__name__}", retryable=True
                ) from exc
            self.calls += 1
            if response.status_code in {401, 403}:
                if attempt == 1 and self._repair():
                    self.refusals += 1
                    continue
                raise KiteError(
                    "the broker refused the session, and repairing it did not help",
                    status_code=response.status_code,
                    error_type="TokenException",
                )
            return response
        raise KiteError("the request was not completed")  # pragma: no cover

    def _repair(self) -> bool:
        try:
            self._session.heal()
        except Exception:  # noqa: BLE001 - a failed repair is reported by the caller
            return False
        return True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"enctoken {self._session.token()}",
            "X-Kite-Version": KITE_VERSION_HEADER[1],
            "User-Agent": "trading-framework-v3/module1",
        }

def _candle(row: Sequence[Any]) -> Candle | None:
    """Turn one Kite candle row into a :class:`Candle`, or drop it."""

    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return None
    try:
        return Candle(
            open_time=parse_timestamp(str(row[0])),
            open=_decimal(row[1]),
            high=_decimal(row[2]),
            low=_decimal(row[3]),
            close=_decimal(row[4]),
            volume=_decimal(row[5]),
        )
    except (ValueError, InvalidOperation, TypeError):
        return None


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _ist_day(candle: Candle) -> date:
    """The exchange session day a candle belongs to."""

    return to_ist(candle.open_time).date()


def _days_at_the_cap(candles: Iterable[Candle]) -> list[date]:
    """Days whose answer returned exactly the cap, and may therefore be cut.

    A day below the cap is complete as far as the broker is concerned, so it
    costs nothing to look. A day *at* the cap is the only one where the tail
    can be hiding.
    """

    counts = Counter(_ist_day(candle) for candle in candles)
    return sorted(day for day, count in counts.items() if count >= MAX_CANDLES_PER_DAY)

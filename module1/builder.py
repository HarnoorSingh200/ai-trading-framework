"""Candles built from the live tick stream.

The websocket is the live edge and the historical API is the record; this is
where the two meet. Ticks are folded into one-minute candles as they arrive, a
bucket is closed when the exchange clock turns the minute over, and the minutes
that never arrived are remembered so a repair pass can fill them.

The rules here were each learned the hard way, and none of them are optional:

* a tick belongs to the minute of its **exchange** timestamp, not of the local
  clock, so a late tick still lands in its own bucket;
* a minute with no tick is recorded as **missing** rather than stitched over -
  otherwise a five-minute bar built from three ticks looks exactly like a real
  one, and an agent cannot tell the difference;
* an out-of-order tick for a minute that has already closed is ignored: the
  closed candle stays authoritative;
* volume is a **difference**, not a sum. The websocket reports cumulative
  traded volume for the day, so a candle's volume is the reading at its close
  minus the reading at its open.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from module1.broker.feed import Tick
from module1.broker.kite import Candle
from module1.timeutil import to_ist, utc_now

#: How many minutes are held per symbol. Roughly seven sessions: enough to
#: build a 60-minute view, bounded so a long session cannot grow for ever.
DEFAULT_KEEP_MINUTES = 2_400


def _floor_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


@dataclass(slots=True)
class _Bucket:
    """The minute currently being built from ticks."""

    symbol: str
    instrument_token: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_at_start: Decimal | None
    volume_at_end: Decimal | None
    ticks: int = 1

    def apply(self, tick: Tick) -> None:
        price = tick.price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if tick.volume is not None:
            self.volume_at_end = tick.volume
        self.ticks += 1

    def to_candle(self) -> Candle:
        volume = Decimal("0")
        if self.volume_at_start is not None and self.volume_at_end is not None:
            volume = max(self.volume_at_end - self.volume_at_start, Decimal("0"))
        return Candle(
            open_time=self.open_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=volume,
            source="live",
        )


def _start_bucket(tick: Tick, symbol: str, open_time: datetime) -> _Bucket:
    return _Bucket(
        symbol=symbol,
        instrument_token=tick.instrument_token,
        open_time=open_time,
        open=tick.price,
        high=tick.price,
        low=tick.price,
        close=tick.price,
        volume_at_start=tick.volume,
        volume_at_end=tick.volume,
    )


@dataclass(slots=True)
class ClosedCandle:
    """A candle this book finished building."""

    symbol: str
    interval: str
    candle: Candle
    ticks: int
    missing_before: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.candle.open_time.isoformat(),
            "close": format(self.candle.close, "f"),
            "volume": format(self.candle.volume, "f"),
            "ticks": self.ticks,
            "source": self.candle.source,
            "missing_before": self.missing_before,
        }


class CandleBook:
    """Holds each symbol's minutes: seeded from history, extended by ticks."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        keep_minutes: int = DEFAULT_KEEP_MINUTES,
        on_closed: Callable[[ClosedCandle], None] | None = None,
    ) -> None:
        self._clock = clock
        self._keep = max(120, int(keep_minutes))
        self._on_closed = on_closed
        self._minutes: dict[str, dict[datetime, Candle]] = {}
        self._forming: dict[str, _Bucket] = {}
        self._missing: dict[str, set[datetime]] = {}
        self._version: dict[str, int] = {}
        self._lock = threading.RLock()
        self.seeded_minutes = 0
        self.built_minutes = 0
        self.closed_events = 0

    # -- seeding from history ------------------------------------------------ #

    def set_on_closed(self, callback: Callable[[ClosedCandle], None] | None) -> None:
        """Attach the consumer of closed candles.

        A setter rather than a constructor argument: the consumer needs the
        book, and the book needs the consumer, so one of them has to go second.
        """

        self._on_closed = callback

    def seed(
        self, symbol: str, candles: Sequence[Candle], *, source: str = "broker"
    ) -> int:
        """Merge historical candles in. Returns how many were new.

        History is the exchange's own record, so it replaces a minute this book
        built from ticks - the two can differ by a few ticks, and the record
        wins. Minutes history does not mention are left exactly as they are.
        """

        key = symbol.upper()
        added = 0
        with self._lock:
            held = self._minutes.setdefault(key, {})
            gaps = self._missing.setdefault(key, set())
            for candle in candles:
                if candle.open_time not in held:
                    added += 1
                held[candle.open_time] = (
                    candle if candle.source == source else replace(candle, source=source)
                )
                # A minute that history has is no longer a gap.
                gaps.discard(candle.open_time)
            self.seeded_minutes += added
            if added:
                self._trim(key, held)
                self._bump(key)
        return added

    # -- the live edge ------------------------------------------------------- #

    def on_tick(self, tick: Tick, symbol: str) -> list[Candle]:
        """Fold one tick in, returning any candle it closed."""

        key = symbol.upper()
        moment = tick.exchange_time or tick.received_at
        opened = _floor_minute(moment)
        closed: list[Candle] = []
        with self._lock:
            bucket = self._forming.get(key)
            if bucket is None:
                self._forming[key] = _start_bucket(tick, key, opened)
                return []
            if opened == bucket.open_time:
                bucket.apply(tick)
                return []
            if opened < bucket.open_time:
                # Out of order: the minute has already closed and been stored.
                return []

            candle = bucket.to_candle()
            missing = self._record_gap(key, bucket.open_time, opened)
            self._store(key, candle)
            closed.append(candle)
            self._forming[key] = _start_bucket(tick, key, opened)
            self.built_minutes += 1
            self.closed_events += 1
            self._bump(key)

        if self._on_closed is not None:
            # A consumer that raises must not cost us the candle we just built.
            with suppress(Exception):
                self._on_closed(
                    ClosedCandle(
                        symbol=key,
                        interval="1m",
                        candle=candle,
                        ticks=bucket.ticks,
                        missing_before=missing,
                    )
                )
        return closed

    def close_all(self) -> list[Candle]:
        """Close every forming bucket, for a shutdown or a session end."""

        with self._lock:
            keys = list(self._forming)
        closed: list[Candle] = []
        for key in keys:
            candle = self.close_symbol(key)
            if candle is not None:
                closed.append(candle)
        return closed

    def close_symbol(self, symbol: str) -> Candle | None:
        key = symbol.upper()
        with self._lock:
            bucket = self._forming.pop(key, None)
            if bucket is None:
                return None
            candle = bucket.to_candle()
            self._store(key, candle)
            self.built_minutes += 1
            self._bump(key)
        return candle

    # -- reads ---------------------------------------------------------------- #

    def minutes(self, symbol: str) -> list[Candle]:
        key = symbol.upper()
        with self._lock:
            held = self._minutes.get(key) or {}
            return [held[open_time] for open_time in sorted(held)]

    def has(self, symbol: str) -> bool:
        with self._lock:
            return bool(self._minutes.get(symbol.upper()))

    def version(self, symbol: str) -> int:
        with self._lock:
            return self._version.get(symbol.upper(), 0)

    def coverage(self, symbol: str) -> dict[str, Any]:
        """What this book holds for one symbol, including what it is missing."""

        key = symbol.upper()
        with self._lock:
            held = self._minutes.get(key) or {}
            gaps = self._missing.get(key) or set()
            if not held:
                return {"symbol": key, "minutes": 0, "sessions": 0, "first": None,
                        "last": None, "missing_minutes": len(gaps)}
            opens = sorted(held)
            sessions = sorted({to_ist(open_time).date() for open_time in opens})
            return {
                "symbol": key,
                "minutes": len(opens),
                "sessions": len(sessions),
                "first": opens[0].isoformat(),
                "last": opens[-1].isoformat(),
                "missing_minutes": len(gaps),
                "session_dates": [day.isoformat() for day in sessions],
            }

    def pending_gaps(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Symbols with missing minutes, worst first."""

        with self._lock:
            rows = [
                {"symbol": symbol, "missing": len(gaps)}
                for symbol, gaps in self._missing.items()
                if gaps
            ]
        rows.sort(key=lambda row: (-row["missing"], row["symbol"]))
        return rows[:limit]

    def pending_repairs(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        lookback_minutes: int = 45,
        settle_seconds: int = 75,
    ) -> list[dict[str, Any]]:
        """Symbols worth asking history about: holes, and unconfirmed minutes.

        A candle built from ticks is not the exchange's record of that minute.
        The stream samples rather than records, so a built minute can be a few
        hundred shares light and can miss the odd tick altogether - measured
        against the real feed, twelve minutes in fifteen were exact and three
        were slightly under on volume.

        History catches up about a minute behind, and when it arrives it should
        replace what was built here. So a minute built from ticks counts as
        *unconfirmed* until history covers it, and repair is what closes that
        loop. Only minutes inside ``lookback_minutes`` are chased: anything
        older than that is not coming.
        """

        moment = now or self._clock()
        oldest = moment - timedelta(minutes=max(1, lookback_minutes))
        newest = moment - timedelta(seconds=max(0, settle_seconds))
        rows: list[dict[str, Any]] = []
        with self._lock:
            for symbol, held in self._minutes.items():
                gaps = len(self._missing.get(symbol) or ())
                unconfirmed = sum(
                    1
                    for open_time, candle in held.items()
                    if candle.source == "live" and oldest <= open_time <= newest
                )
                if gaps or unconfirmed:
                    rows.append(
                        {"symbol": symbol, "missing": gaps, "unconfirmed": unconfirmed}
                    )
        rows.sort(key=lambda row: (-(row["missing"] + row["unconfirmed"]), row["symbol"]))
        return rows[:limit]

    def counted_sources(self, symbol: str) -> dict[str, int]:
        """How many minutes came from ticks and how many from history."""

        with self._lock:
            held = self._minutes.get(symbol.upper()) or {}
            counted: dict[str, int] = {}
            for candle in held.values():
                counted[candle.source] = counted.get(candle.source, 0) + 1
        return counted

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._minutes)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            forming = len(self._forming)
            held = sum(len(minutes) for minutes in self._minutes.values())
            gaps = sum(len(day) for day in self._missing.values())
        return {
            "symbols": len(self._minutes),
            "minutes_held": held,
            "forming": forming,
            "missing_minutes": gaps,
            "seeded_minutes": self.seeded_minutes,
            "built_minutes": self.built_minutes,
            "closed_events": self.closed_events,
        }

    # -- internals ------------------------------------------------------------ #

    def _store(self, symbol: str, candle: Candle) -> None:
        held = self._minutes.setdefault(symbol, {})
        held[candle.open_time] = candle
        (self._missing.setdefault(symbol, set())).discard(candle.open_time)
        self._trim(symbol, held)

    def _trim(self, symbol: str, held: dict[datetime, Candle]) -> None:
        if len(held) <= self._keep:
            return
        for open_time in sorted(held)[: len(held) - self._keep]:
            held.pop(open_time, None)

    def _record_gap(self, symbol: str, previous: datetime, current: datetime) -> int:
        """Note the minutes between two buckets that produced no tick at all."""

        gaps = self._missing.setdefault(symbol, set())
        cursor = previous + timedelta(minutes=1)
        missing = 0
        while cursor < current:
            gaps.add(cursor)
            missing += 1
            cursor += timedelta(minutes=1)
        return missing

    def _bump(self, symbol: str) -> None:
        self._version[symbol] = self._version.get(symbol, 0) + 1


def minutes_in(candles: Iterable[Candle]) -> int:
    """Small helper for reporting: how many minutes a series covers."""

    return len(list(candles))

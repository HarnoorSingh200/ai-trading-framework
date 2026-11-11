"""Indicator values for every symbol, kept fresh as candles close.

Two kinds of value live here, and they refresh on different clocks:

* **Session constants** - the previous completed session's high, low and
  close, the seven pivot levels, and the distance from the last close to the
  nearest of them. They change once a day, so they are computed once per
  symbol per session date and cached.
* **Bar-driven values** - everything else, recomputed when a bar of that
  timeframe closes. Never on a forming candle: a value that flickers is worse
  than one that is a minute old, because you cannot tell which you are reading.

Nothing here runs on a request path. A closed candle makes a symbol's frames
stale, a background pass recomputes them, and a reader gets whatever is
current along with the bar it belongs to.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from module1.builder import CandleBook
from module1.candles import TIMEFRAMES, CandleService
from module1.indicators import (
    INDICATOR_VERSION,
    DayLevels,
    FieldSpec,
    IndicatorParameters,
    catalogue,
    compute_latest,
)
from module1.timeutil import to_ist, utc_now

#: Timeframes that get pivot levels. Levels are intraday reference points; a
#: daily chart showing a previous day's pivots is a category error.
LEVEL_TIMEFRAMES: frozenset[str] = frozenset(
    spec.value for spec in TIMEFRAMES if spec.value != "1d"
)


@dataclass(slots=True)
class _Frame:
    """The computed values for one symbol and timeframe, and their bar."""

    version: int
    values: dict[str, Any]
    bar_open_time: str | None


class IndicatorService:
    """Owns the indicator frames for the universe."""

    def __init__(
        self,
        *,
        candles: CandleService,
        book: CandleBook,
        parameters: IndicatorParameters | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._candles = candles
        self._book = book
        self._parameters = parameters or IndicatorParameters()
        self._clock = clock
        self._frames: dict[tuple[str, str], _Frame] = {}
        #: The same values for the last bar that has actually *finished*.
        #: A live frame follows the bar being built, which is what the table
        #: wants; an agent waking on a bar close wants the bar before it, and
        #: the two must never be confused. Kept separately rather than replaced
        #: so the board's behaviour does not change.
        self._closed: dict[tuple[str, str], _Frame] = {}
        self._levels: dict[tuple[str, str], DayLevels | None] = {}
        self._refreshed: dict[str, int] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self.refreshes = 0
        self.failures = 0
        self.last_error: str | None = None

    # -- reads --------------------------------------------------------------- #

    def latest(self, symbol: str, interval: str) -> dict[str, Any] | None:
        """The current values for one symbol and timeframe, or ``None``."""

        with self._lock:
            frame = self._frames.get((symbol.upper(), interval))
        if frame is None:
            return None
        return {"values": frame.values, "bar_open_time": frame.bar_open_time}

    def closed_latest(self, symbol: str, interval: str) -> dict[str, Any] | None:
        """The same, for the last bar that has finished.

        ``None`` when no bar of that timeframe has closed yet - the first
        minutes of a session, before the opening bucket ends.
        """

        with self._lock:
            frame = self._closed.get((symbol.upper(), interval))
        if frame is None:
            return None
        return {"values": frame.values, "bar_open_time": frame.bar_open_time}

    def closed_frame(self, symbol: str, interval: str) -> dict[str, Any] | None:
        """The whole frame - values, levels, groups - for the last finished bar.

        This is what a decision is allowed to be based on. ``None`` means no bar
        of that timeframe has finished yet, which is a different answer from
        "the values are null": there is nothing to decide about at all.
        """

        key = symbol.upper()
        with self._lock:
            frame = self._closed.get((key, interval))
        if frame is None:
            return None
        return self.frame(key, interval, closed=True)

    def levels_for(self, symbol: str, *, now: datetime | None = None) -> DayLevels | None:
        """The previous session's levels, computed once per session date."""

        key = symbol.upper()
        moment = now or self._clock()
        day = to_ist(moment).date().isoformat()
        cache_key = (key, day)
        with self._lock:
            if cache_key in self._levels:
                return self._levels[cache_key]
        prior = self._candles.previous_session(key, now=moment)
        levels = (
            None
            if prior is None
            else DayLevels(
                high=prior.high,
                low=prior.low,
                close=prior.close,
                session_date=to_ist(prior.open_time).date().isoformat(),
            )
        )
        with self._lock:
            self._levels[cache_key] = levels
        return levels

    def frame(self, symbol: str, interval: str, *, closed: bool = False) -> dict[str, Any]:
        """The published indicator frame for one symbol and timeframe.

        ``closed=True`` answers for the last finished bar instead of the one
        being built, which is what a decision made *because* a bar closed has
        to be based on.
        """

        key = symbol.upper()
        source = self._closed if closed else self._frames
        with self._lock:
            frame = source.get((key, interval))
        values = {} if frame is None else frame.values
        levels = self.levels_for(key) if interval in LEVEL_TIMEFRAMES else None
        found = None
        if levels is not None and values.get("level_distance_name"):
            pivots = levels.pivots()
            name = str(values["level_distance_name"])
            found = {
                "name": name,
                "value": float(pivots[name]) if name in pivots else None,
                "distance_pct": values.get("level_distance_pct"),
            }
        return {
            "symbol": key,
            "interval": interval,
            "as_of": (self._clock()).isoformat(),
            "bar_open_time": None if frame is None else frame.bar_open_time,
            "indicator_version": INDICATOR_VERSION,
            "parameters": self._parameters.describe(),
            "day_levels": None if levels is None else levels.describe(),
            "nearest_level": found,
            "groups": _group(values),
            "values": values,
        }

    def describe(self) -> dict[str, Any]:
        symbols = self._book.symbols()
        with self._lock:
            stale = sum(
                1 for symbol in symbols if self._refreshed.get(symbol) != self._book.version(symbol)
            )
            frames = len(self._frames)
            closed_frames = len(self._closed)
            #: How many symbols have at least one computed frame. ``frames``
            #: counts symbol-timeframe pairs, so it is the wrong number to
            #: compare against the universe; this is the one a readiness gate
            #: can ask about without confusing a wide symbol with a covered
            #: universe.
            symbols_done = len({symbol for symbol, _ in self._frames})
        return {
            "version": INDICATOR_VERSION,
            "symbols": len(symbols),
            "symbols_done": symbols_done,
            "frames": frames,
            "closed_frames": closed_frames,
            "stale_symbols": stale,
            "refreshes": self.refreshes,
            "failures": self.failures,
            "last_error": self.last_error,
            "parameters": self._parameters.describe(),
        }

    @staticmethod
    def catalogue() -> dict[str, Any]:
        return catalogue()

    # -- the refresh loop ---------------------------------------------------- #

    def note_closed(self, symbol: str) -> None:
        """A candle closed: ask the refresher to look again soon.

        The work is not done here. This runs on the socket's reader thread,
        where the only thing that matters is getting back to reading ticks.
        """

        self._wake.set()

    def wait(self, timeout: float) -> bool:
        """Block until a candle closes, or the timeout expires."""

        return self._wake.wait(timeout)

    def clear_wake(self) -> None:
        self._wake.clear()

    def refresh_symbol(
        self, symbol: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Recompute every timeframe's frame for one symbol."""

        key = symbol.upper()
        moment = now or self._clock()
        levels = self.levels_for(key, now=moment)
        updated: list[str] = []
        for spec in TIMEFRAMES:
            try:
                bars = self._candles.bars(key, spec.value, now=moment, refresh_stale=True)
            except Exception as exc:  # noqa: BLE001 - one timeframe must not stop the rest
                self.failures += 1
                self.last_error = f"{key} {spec.value}: {type(exc).__name__}: {exc}"[:200]
                continue
            if not bars:
                continue
            values = compute_latest(
                bars,
                parameters=self._parameters,
                day_levels=levels if spec.value in LEVEL_TIMEFRAMES else None,
            )
            if values is None:
                continue
            last = bars[-1]
            # The bar being built is not a bar anyone may act on. Its values are
            # still published - the table shows them - but the frame for the
            # bar that has actually finished is kept beside it, and is the one a
            # screening or a decision reads.
            if last.is_forming:
                finished = [bar for bar in bars if not bar.is_forming]
                closed_values = (
                    compute_latest(
                        finished,
                        parameters=self._parameters,
                        day_levels=levels if spec.value in LEVEL_TIMEFRAMES else None,
                    )
                    if finished
                    else None
                )
            else:
                finished = bars
                closed_values = values
            with self._lock:
                self._frames[(key, spec.value)] = _Frame(
                    version=self._book.version(key),
                    values=values,
                    bar_open_time=last.open_time.isoformat(),
                )
                if closed_values is None:
                    self._closed.pop((key, spec.value), None)
                else:
                    self._closed[(key, spec.value)] = _Frame(
                        version=self._book.version(key),
                        values=closed_values,
                        bar_open_time=finished[-1].open_time.isoformat(),
                    )
            updated.append(spec.value)
        with self._lock:
            self._refreshed[key] = self._book.version(key)
            self.refreshes += 1
        return {"symbol": key, "timeframes": updated, "levels": levels is not None}

    def refresh_stale(self, *, limit: int = 8, now: datetime | None = None) -> dict[str, Any]:
        """Bring a slice of the universe up to date. Cheap enough to call often."""

        with self._lock:
            stale = [
                symbol
                for symbol in self._book.symbols()
                if self._refreshed.get(symbol) != self._book.version(symbol)
            ]
        done: list[str] = []
        for symbol in stale[: max(1, limit)]:
            self.refresh_symbol(symbol, now=now)
            done.append(symbol)
        return {
            "stale": len(stale),
            "refreshed": len(done),
            "symbols": done,
        }

    def known_symbols(self) -> list[str]:
        with self._lock:
            return sorted({symbol for symbol, _ in self._frames})


def _group(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Split flat values into the groups the UI renders them under."""

    from module1.indicators import FIELDS

    grouped: dict[str, dict[str, Any]] = {}
    for spec in FIELDS:
        if spec.name in values:
            grouped.setdefault(spec.group, {})[spec.name] = values[spec.name]
    return grouped


def fields_for(group: str) -> Sequence[FieldSpec]:
    from module1.indicators import FIELDS

    return [spec for spec in FIELDS if spec.group == group]

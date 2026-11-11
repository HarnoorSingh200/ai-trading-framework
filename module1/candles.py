"""Candles per timeframe, with the change and volume each one carries.

The broker is asked for **one**-minute candles only. Everything else - 5, 10,
15, 20, 30 and 60 minutes - is aggregated here, from that one base. Deriving
rather than fetching is not an optimisation: a 15-minute candle that came from
a separate broker request can disagree with the three 1-minute candles inside
it, and then two parts of the same framework are looking at two different
markets. Daily candles are the exception and are fetched directly, because
deriving them here would mean holding months of one-minute data.

Buckets align to the exchange session open, not to the clock hour: the first
15-minute candle of the day is 09:15-09:30, which is what the exchange's own
charts show.

Every candle is returned with:

* ``change_percent`` - close against the **previous candle's close**, which is
  the honest "how much did this candle change" across a continuous series;
* ``move_percent`` - close against its own **open**, i.e. the candle's body;
* ``volume``, and ``volume_ratio`` against the average of the window, which is
  what makes a volume number mean anything on its own.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any, Final

from module1.broker.kite import Candle, KiteClient
from module1.builder import CandleBook
from module1.session_clock import MARKET_CLOSE, MARKET_OPEN, default_calendar
from module1.timeutil import IST, format_ist, to_ist, utc_now

#: Default window handed to the UI inspector.
DEFAULT_LIMIT: Final[int] = 60
MAX_LIMIT: Final[int] = 300

#: Sessions of one-minute data held per symbol. Enough for a readable 60-minute
#: view (three sessions is 75 bars) without spending minutes on the broker.
DEFAULT_SESSIONS: Final[int] = 3
MAX_SESSIONS: Final[int] = 8

#: How long a fetched one-minute series is reused before it is fetched again.
DEFAULT_CACHE_SECONDS: Final[float] = 45.0

#: How far back a repair pass is willing to look for a minute to confirm.
#: History publishes about a minute behind the market; minutes older than this
#: are not going to be republished, so chasing them would be a request a
#: minute for ever.
REPAIR_LOOKBACK_MINUTES: Final[int] = 45

#: How much recent history one repair request asks for. History runs about a
#: minute behind the market and a pass runs every few minutes, so fifteen
#: minutes covers both with room for a delayed pass - and stays far below the
#: 360-candle cap, so it is one request with no follow-up probe.
REPAIR_WINDOW_MINUTES: Final[int] = 15

#: Daily candles are cached far longer than a minute series: they change once a
#: session, and a fifty-symbol refresh of them is fifty requests.
DAY_CACHE_SECONDS: Final[float] = 600.0

#: How far back the daily series is fetched. One depth for every caller: two
#: callers wanting different depths cannot share a cache keyed only by symbol,
#: and the first one to ask would otherwise shrink the answer for the second.
#: Ninety calendar days is about sixty trading sessions.
DAY_SERIES_SESSIONS: Final[int] = 90

#: How many recent closes ride along with each row of a timeframe board, so the
#: table can draw a shape instead of only a number. Twenty-four bars is about a
#: session on 15m and a month on 1D.
SPARK_BARS: Final[int] = 24


@dataclass(frozen=True, slots=True)
class TimeframeSpec:
    """One selectable timeframe."""

    value: str
    label: str
    minutes: int
    #: The interval name the broker knows, or ``None`` for a derived one.
    kite: str | None

    @property
    def derived(self) -> bool:
        return self.kite is None

    def describe(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "minutes": self.minutes,
            "derived": self.derived,
        }


#: Every timeframe the module offers, in ascending order.
TIMEFRAMES: Final[tuple[TimeframeSpec, ...]] = (
    TimeframeSpec("1m", "1 minute", 1, "minute"),
    TimeframeSpec("5m", "5 minutes", 5, None),
    TimeframeSpec("10m", "10 minutes", 10, None),
    TimeframeSpec("15m", "15 minutes", 15, None),
    TimeframeSpec("20m", "20 minutes", 20, None),
    TimeframeSpec("30m", "30 minutes", 30, None),
    TimeframeSpec("60m", "60 minutes", 60, None),
    TimeframeSpec("1d", "1 day", 1440, "day"),
)

TIMEFRAME_BY_VALUE: Final[dict[str, TimeframeSpec]] = {
    spec.value: spec for spec in TIMEFRAMES
}


class TimeframeError(ValueError):
    """An unknown timeframe was requested."""

    def __init__(self, value: str) -> None:
        super().__init__(f"unknown timeframe {value!r}")
        self.value = value
        self.supported = [spec.value for spec in TIMEFRAMES]


class SymbolUnresolved(LookupError):
    """The symbol has no instrument token, so there is nothing to fetch."""

    def __init__(self, symbol: str) -> None:
        super().__init__(f"{symbol} has no resolved instrument token")
        self.symbol = symbol


def timeframe_for(value: str) -> TimeframeSpec:
    """The spec for ``value``, or a refusal that lists what is offered."""

    spec = TIMEFRAME_BY_VALUE.get(str(value))
    if spec is None:
        raise TimeframeError(value)
    return spec


@dataclass(slots=True)
class Bar:
    """One aggregated candle, with the numbers a person actually reads."""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    #: True only when the period has ended *and* every expected minute is
    #: present. A bar that is short of minutes is not a real candle of that
    #: size, and calling it complete would be its own kind of lie.
    is_complete: bool
    #: The period has not finished yet.
    is_forming: bool
    #: The period ended but minutes are missing from the source.
    is_gappy: bool
    #: 1-minute candles that went into this bar, and how many were expected.
    minutes_present: int
    minutes_expected: int

    symbol: str = ""
    interval: str = ""
    #: Where the minutes behind this bar came from: "live" when every one of
    #: them was built from the tick stream, "broker" when they all came from
    #: history, "mixed" when both are present. This is what makes a live bar
    #: checkable against the exchange's own record.
    source: str = "broker"
    change: Decimal | None = None
    change_percent: Decimal | None = None
    move_percent: Decimal | None = None
    range_percent: Decimal | None = None
    volume_ratio: Decimal | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "open_time_ist": format_ist(self.open_time),
            "open": _price(self.open),
            "high": _price(self.high),
            "low": _price(self.low),
            "close": _price(self.close),
            "volume": _volume(self.volume),
            "is_complete": self.is_complete,
            "is_forming": self.is_forming,
            "is_gappy": self.is_gappy,
            "source": self.source,
            "change": _price(self.change),
            "change_percent": _percent(self.change_percent),
            "move_percent": _percent(self.move_percent),
            "range_percent": _percent(self.range_percent),
            "volume_ratio": _ratio(self.volume_ratio),
            "minutes_present": self.minutes_present,
            "minutes_expected": self.minutes_expected,
        }


#: Session opens, keyed by the IST date they belong to. Aggregation asks for
#: one of these per candle, and there are only ever a handful of distinct
#: answers in a window - so asking repeatedly was pure waste.
_SESSION_OPENS: dict[date, datetime] = {}


def session_open_for(moment: datetime) -> datetime:
    """The exchange session open on the calendar day ``moment`` falls in."""

    day = to_ist(moment).date()
    opened = _SESSION_OPENS.get(day)
    if opened is None:
        opened = datetime.combine(day, MARKET_OPEN, tzinfo=IST)
        _SESSION_OPENS[day] = opened
    return opened


def session_aligned_start(now: datetime, *, days_back: int) -> datetime:
    """Midnight IST, ``days_back`` days before ``now``.

    Anchoring a fetch to midnight rather than to "now minus N days" is what
    keeps the oldest session in the window whole: starting four days back at
    13:42 would quietly hand back a first day that begins at lunchtime.
    """

    day = to_ist(now).date() - timedelta(days=max(0, int(days_back)))
    return datetime.combine(day, clock_time(0, 0), tzinfo=IST)


#: Minutes in a regular session: 09:15 to 15:30.
SESSION_MINUTES: Final[int] = int(
    (MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute)
    - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
)

#: Minutes a session's one-minute *history* actually contains: 09:15 to 15:14.
#: The broker's minute series for the cash segment stops at 15:14 - the exchange
#: publishes 360 minutes and no more - so the last bar a day can build is the
#: bucket holding 15:14. Naming the 15:15-15:30 bucket instead would describe a
#: candle with no minutes behind it, which is why every symbol would come back
#: as missing from it. Live ticks do run to 15:29, but a bucket nobody can
#: confirm against the exchange's own record is not one to decide on.
AVAILABLE_MINUTES: Final[int] = 360


def previous_trading_day(day: date) -> date:
    """The most recent session day before ``day``.

    Weekends and exchange holidays are skipped, using the session clock's
    holiday list: the morning after a holiday, the last closed bucket is the
    one the previous session produced, not a bucket on a day the exchange was
    shut.
    """

    calendar = default_calendar()
    earlier = day - timedelta(days=1)
    for _ in range(3660):
        if earlier.weekday() < 5 and not calendar.is_holiday(earlier):
            return earlier
        earlier -= timedelta(days=1)
    return earlier


def last_closed_bucket(
    now: datetime, spec: TimeframeSpec
) -> tuple[datetime, datetime]:
    """The bucket of ``spec`` that has just finished, as ``(open, close)``.

    Answered from the exchange clock rather than from a cache, so every caller
    is talking about the same candle: the one that just closed. Before the
    first bucket of a session has ended - and after the close - the answer is
    the previous session's last bucket, which is a real closed candle rather
    than a guess.

    Daily candles are their own case: one observation per session, stamped at
    midnight IST, closing with the exchange rather than at midnight.
    """

    day = to_ist(now).date()
    # A weekend or an exchange holiday is not a session, so it cannot have
    # closed a candle of its own. The session clock already models this;
    # naming a Saturday or Diwali bucket here would report every symbol as
    # missing from a bar that never existed.
    calendar = default_calendar()
    while day.weekday() >= 5 or calendar.is_holiday(day):
        day = previous_trading_day(day)
    if spec.minutes >= 1440:
        close = datetime.combine(day, MARKET_CLOSE, tzinfo=IST)
        if close > now:
            day = previous_trading_day(day)
        return datetime.combine(day, clock_time(0, 0), tzinfo=IST), datetime.combine(
            day, MARKET_CLOSE, tzinfo=IST
        )

    step = timedelta(minutes=spec.minutes)
    # The last bucket of a session is the one holding its final *published*
    # minute, 15:14. For timeframes that do not divide 360 evenly that bucket
    # runs past the closing bell, which is exactly what makes a short last bar
    # visible rather than silently dropped.
    final_index = (AVAILABLE_MINUTES - 1) // spec.minutes
    while True:
        opened = datetime.combine(day, MARKET_OPEN, tzinfo=IST)
        elapsed = int((to_ist(now) - opened).total_seconds() // 60)
        if elapsed < 0 or elapsed // spec.minutes == 0:
            # Nothing of this session has finished yet.
            day = previous_trading_day(day)
            continue
        index = min(elapsed // spec.minutes - 1, final_index)
        start = opened + index * step
        return start, start + step


def aggregate(
    base: Sequence[Candle],
    spec: TimeframeSpec,
    *,
    symbol: str,
    now: datetime | None = None,
) -> list[Bar]:
    """Fold one-minute candles into ``spec`` buckets, aligned to the session."""

    if spec.minutes <= 1:
        return _bars_from_minute(base, spec=spec, symbol=symbol, now=now)
    moment = now or utc_now()
    buckets: dict[datetime, list[Candle]] = {}
    for candle in sorted(base, key=lambda item: item.open_time):
        opened = session_open_for(candle.open_time)
        offset = int((candle.open_time - opened).total_seconds() // 60)
        if offset < 0:
            # A candle stamped before the session open cannot be bucketed
            # against it; align it to itself rather than dropping it.
            start = candle.open_time
        else:
            start = opened + timedelta(minutes=(offset // spec.minutes) * spec.minutes)
        buckets.setdefault(start, []).append(candle)

    step = timedelta(minutes=spec.minutes)
    # A daily candle is one observation from the broker, not 1440 minutes
    # assembled here, and it closes with the session rather than at midnight.
    daily = spec.minutes >= 1440
    expected = 1 if spec.kite else spec.minutes
    bars: list[Bar] = []
    for start in sorted(buckets):
        members = buckets[start]
        end = (
            datetime.combine(to_ist(start).date(), MARKET_CLOSE, tzinfo=IST)
            if daily
            else start + step
        )
        present = len(members)
        forming = end > moment
        gappy = not forming and present < expected
        live = sum(1 for member in members if member.source == "live")
        origin = "live" if live == present else ("mixed" if live else "broker")
        bars.append(
            Bar(
                open_time=start,
                close_time=end,
                open=members[0].open,
                high=max(item.high for item in members),
                low=min(item.low for item in members),
                close=members[-1].close,
                volume=sum((item.volume for item in members), Decimal("0")),
                is_complete=not forming and not gappy,
                is_forming=forming,
                is_gappy=gappy,
                minutes_present=present,
                minutes_expected=expected,
                symbol=symbol,
                interval=spec.value,
                source=origin,
            )
        )
    return _with_metrics(bars)


def _bars_from_minute(
    base: Sequence[Candle], *, spec: TimeframeSpec, symbol: str, now: datetime | None
) -> list[Bar]:
    moment = now or utc_now()
    bars: list[Bar] = []
    for candle in sorted(base, key=lambda item: item.open_time):
        end = candle.open_time + timedelta(minutes=spec.minutes)
        forming = end > moment
        bars.append(
            Bar(
                open_time=candle.open_time,
                close_time=end,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                is_complete=not forming,
                is_forming=forming,
                is_gappy=False,
                minutes_present=1,
                minutes_expected=1,
                symbol=symbol,
                interval=spec.value,
                source=candle.source,
            )
        )
    return _with_metrics(bars)


def _with_metrics(bars: list[Bar]) -> list[Bar]:
    """Add the per-candle change and volume numbers, in place."""

    if not bars:
        return bars
    volumes = [bar.volume for bar in bars if bar.volume > 0]
    average = (
        sum(volumes, Decimal("0")) / Decimal(len(volumes)) if volumes else Decimal("0")
    )
    previous_close: Decimal | None = None
    for bar in bars:
        if previous_close is not None and previous_close > 0:
            bar.change = bar.close - previous_close
            bar.change_percent = bar.change / previous_close * 100
        if bar.open > 0:
            bar.move_percent = (bar.close - bar.open) / bar.open * 100
        if bar.low > 0:
            bar.range_percent = (bar.high - bar.low) / bar.low * 100
        if average > 0:
            bar.volume_ratio = bar.volume / average
        previous_close = bar.close
    return bars


@dataclass(slots=True)
class _CacheEntry:
    fetched_at: datetime
    candles: list[Candle]


@dataclass(slots=True)
class _DerivedEntry:
    """Aggregated bars for one symbol and timeframe, and what built them."""

    version: int
    bars: list[Bar]


class CandleService:
    """Serves candle series: from the live book first, history only to seed it.

    The order of preference is the whole point. Once a symbol has been seeded,
    every timeframe comes from candles the process already holds - built from
    ticks, or merged in from a previous fetch - and the broker is touched only
    to *extend* a series that is too short, or to fill a gap the websocket
    missed. That is what keeps a live session off the historical API.
    """

    def __init__(
        self,
        client: KiteClient,
        *,
        resolve_token: Callable[[str], str | None],
        book: CandleBook | None = None,
        symbol_meta: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] = utc_now,
        sessions: int = DEFAULT_SESSIONS,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._resolve = resolve_token
        self._book = book if book is not None else CandleBook(clock=clock)
        self._meta = symbol_meta or (lambda _symbol: {})
        self._clock = clock
        self._sessions = max(1, min(int(sessions), MAX_SESSIONS))
        self._cache_seconds = max(0.0, float(cache_seconds))
        self._sleep = sleeper
        self._extended_at: dict[str, datetime] = {}
        self._days: dict[str, _CacheEntry] = {}
        self._derived: dict[tuple[str, str], _DerivedEntry] = {}
        self._indicators: Callable[[str, str], dict[str, Any] | None] | None = None
        self._closed_frame: Callable[[str, str], dict[str, Any] | None] | None = None
        self._indicator_status: Callable[[], dict[str, Any]] | None = None
        self._lock = threading.RLock()
        self.fetches = 0
        self.cache_hits = 0
        self.aggregations = 0

    @property
    def book(self) -> CandleBook:
        return self._book

    def set_indicator_provider(
        self,
        latest: Callable[[str, str], dict[str, Any] | None],
        describe: Callable[[], dict[str, Any]] | None = None,
        closed_frame: Callable[[str, str], dict[str, Any] | None] | None = None,
    ) -> None:
        """Attach the indicator layer, the way the board attaches its feed.

        The provider is a callback rather than a direct import so this module
        keeps knowing nothing about indicators.

        ``closed_frame`` answers for the last bar that has *finished*, which is
        the only bar a screen or a decision may be built on. The live frame
        stays attached beside it because the table reads that one.
        """

        self._indicators = latest
        self._indicator_status = describe
        self._closed_frame = closed_frame

    def _finished_frame(self, symbol: str, interval: str) -> dict[str, Any] | None:
        if self._closed_frame is None:
            return None
        return self._closed_frame(symbol, interval)

    def _finished_bar(self, symbol: str, spec: TimeframeSpec, *, now: datetime) -> Bar | None:
        """The newest bar of ``spec`` whose period has actually ended."""

        bars = self._board_bars(symbol, spec, now=now)
        finished = [bar for bar in bars if not bar.is_forming and bar.close_time <= now]
        return finished[-1] if finished else None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "sessions": self._sessions,
                "cache_seconds": self._cache_seconds,
                "symbols_held": len(self._book.symbols()),
                "fetches": self.fetches,
                "cache_hits": self.cache_hits,
                "timeframes": [spec.value for spec in TIMEFRAMES],
                "book": self._book.stats(),
                "derived": {
                    "aggregations": self.aggregations,
                    "entries": len(self._derived),
                },
                "indicators": (
                    None if self._indicator_status is None else self._indicator_status()
                ),
                "pending_gaps": self._book.pending_gaps(limit=5),
                "pending_repairs": self._book.pending_repairs(limit=5),
            }

    def invalidate(self, symbol: str | None = None) -> None:
        with self._lock:
            self._days.clear()
            if symbol is None:
                self._derived.clear()
            else:
                key = symbol.upper()
                self._extended_at.pop(key, None)
                for cache_key in [pair for pair in self._derived if pair[0] == key]:
                    self._derived.pop(cache_key, None)

    def series(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = DEFAULT_LIMIT,
        interactive: bool = True,
    ) -> dict[str, Any]:
        """The last ``limit`` candles for one symbol on one timeframe."""

        spec = TIMEFRAME_BY_VALUE.get(interval)
        if spec is None:
            raise TimeframeError(interval)
        key = symbol.strip().upper()
        token = self._resolve(key)
        if not token:
            raise SymbolUnresolved(key)
        bounded = max(1, min(int(limit), MAX_LIMIT))
        now = self._clock()

        if spec.value == "1d":
            candles = self._day_candles(
                key, token, now=now, interactive=interactive
            )
            bars = aggregate(candles, spec, symbol=key, now=now)
        else:
            source = "book"
            bars = self._bars(key, spec, now=now)
            if self._should_extend(key, held=len(bars), limit=bounded, now=now):
                self._seed(key, token, now=now, sessions=self._sessions,
                           interactive=interactive)
                bars = self._bars(key, spec, now=now)
                source = "history"
            else:
                self.cache_hits += 1
            if not bars:
                raise SymbolUnresolved(f"{key} has no candles")

        window = bars[-bounded:]
        return {
            "symbol": key,
            "interval": spec.value,
            "interval_label": spec.label,
            "source": "broker" if spec.value == "1d" else "derived from 1m",
            "served_from": "broker" if spec.value == "1d" else source,
            "count": len(window),
            "sessions": self._sessions,
            "as_of": now.isoformat(),
            "coverage": self._book.coverage(key) if spec.value != "1d" else None,
            "summary": _summarise(window),
            "candles": [bar.describe() for bar in window],
        }

    # -- seeding, extension and repair -------------------------------------- #

    def warm(
        self,
        symbols: Iterable[str],
        *,
        sessions: int = 1,
        max_workers: int = 4,
    ) -> dict[str, Any]:
        """Seed the book for many symbols, in parallel, on the background lane.

        This is the expensive part of a start-up - one or two requests per
        symbol - so it runs off the critical path: the board is served as soon
        as the day series lands, and a person opening a symbol is served from
        the interactive lane rather than waiting behind this.
        """

        targets = [str(symbol).upper() for symbol in symbols]
        seeded: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futures = {
                pool.submit(self._warm_one, symbol, sessions): symbol
                for symbol in targets
            }
            for future, symbol in futures.items():
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
                    failed.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:150]})
                    continue
                (seeded if outcome else skipped).append(symbol)
        return {
            "requested": len(targets),
            "seeded": len(seeded),
            "already_held": len(skipped),
            "failed": failed,
        }

    def repair(self, *, limit: int = 10) -> dict[str, Any]:
        """Fill the holes, and confirm what was built from ticks.

        Two jobs, one pass. The obvious one is a minute the websocket never
        delivered. The less obvious one is a minute it *did* deliver: the feed
        samples trades rather than recording them, so a candle built from ticks
        can be a little light on volume and can miss a high or a low entirely.
        History is the exchange's own record, and when it catches up (about a
        minute behind) it replaces what was built here.

        Only symbols that need one of those two things are touched, and the
        window asked for is minutes rather than days.
        """

        now = self._clock()
        pending = self._book.pending_repairs(limit=limit, now=now)
        repaired: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for row in pending:
            symbol = row["symbol"]
            token = self._resolve(symbol)
            if not token:
                continue
            before = self._book.counted_sources(symbol)
            try:
                self._confirm(symbol, token, now=now, lookback_minutes=REPAIR_LOOKBACK_MINUTES)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                failed.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:150]})
                continue
            after = self._book.counted_sources(symbol)
            repaired.append(
                {
                    "symbol": symbol,
                    "missing_before": row["missing"],
                    "unconfirmed_before": row["unconfirmed"],
                    "live_minutes_before": before.get("live", 0),
                    "live_minutes_after": after.get("live", 0),
                }
            )
        return {"considered": len(pending), "repaired": repaired, "failed": failed}

    # -- internals ----------------------------------------------------------- #

    def _bars(
        self,
        symbol: str,
        spec: TimeframeSpec,
        *,
        now: datetime,
        refresh_stale: bool = True,
    ) -> list[Bar]:
        """Aggregated bars for one symbol and timeframe, cached until it changes.

        Rebuilding these on every request was the most expensive thing Module 1
        did: fifty symbols of about a thousand minutes each, several times a
        second, always to keep one bar at the end. The book's version counter
        moves whenever a minute is stored or replaced - including by a repair -
        so it is exactly the right invalidation key.
        """

        key = (symbol, spec.value)
        version = self._book.version(symbol)
        with self._lock:
            entry = self._derived.get(key)
            if entry is not None:
                fresh = entry.version == version and not _forming_has_ended(
                    entry.bars, now
                )
                if fresh:
                    self.cache_hits += 1
                    return entry.bars
                if not refresh_stale:
                    # Someone is waiting on this: hand back what we have and let
                    # the background refresher catch up, rather than making the
                    # request pay for fifty symbols of re-aggregation.
                    return entry.bars

        minutes = self._book.minutes(symbol)
        bars = aggregate(minutes, spec, symbol=symbol, now=now) if minutes else []
        with self._lock:
            self._derived[key] = _DerivedEntry(version=version, bars=bars)
            self.aggregations += 1
        return bars

    def _should_extend(
        self, symbol: str, *, held: int, limit: int, now: datetime
    ) -> bool:
        """Whether history is worth fetching for this symbol right now."""

        if not self._book.has(symbol):
            return True
        if held >= limit:
            return False
        coverage = self._book.coverage(symbol)
        if coverage["sessions"] >= self._sessions:
            return False
        last = self._extended_at.get(symbol)
        return last is None or (now - last).total_seconds() > self._cache_seconds

    def _seed(
        self,
        symbol: str,
        token: str,
        *,
        now: datetime,
        sessions: int,
        interactive: bool,
    ) -> int:
        candles = self._client.historical(
            token,
            "1m",
            start=session_aligned_start(now, days_back=sessions + 3),
            end=now,
            interactive=interactive,
        )
        with self._lock:
            self.fetches += 1
            self._extended_at[symbol] = now
        return self._book.seed(symbol, candles)

    def _confirm(
        self, symbol: str, token: str, *, now: datetime, lookback_minutes: int
    ) -> int:
        """Fetch a short recent window and let history replace what we built."""

        start = session_aligned_start(now, days_back=0)
        window_start = max(
            start,
            now - timedelta(minutes=max(2, min(lookback_minutes, REPAIR_WINDOW_MINUTES))),
        )
        candles = self._client.historical(
            token, "1m", start=window_start, end=now, interactive=False
        )
        with self._lock:
            self.fetches += 1
        return self._book.seed(symbol, candles)

    def _warm_one(self, symbol: str, sessions: int) -> bool:
        token = self._resolve(symbol)
        if not token:
            raise SymbolUnresolved(symbol)
        coverage = self._book.coverage(symbol)
        if coverage["sessions"] >= max(1, sessions):
            return False
        self._seed(
            symbol, token, now=self._clock(), sessions=max(1, sessions), interactive=False
        )
        return True

    def seconds_since_fetch(self, symbol: str) -> float | None:
        with self._lock:
            last = self._extended_at.get(symbol.upper())
        return None if last is None else (self._clock() - last).total_seconds()

    # -- the whole universe on one timeframe -------------------------------- #

    def timeframe_board(self, interval: str) -> dict[str, Any]:
        """Every symbol's latest candle on one timeframe.

        This is the view the timeframe tabs read: one row per stock, holding
        that stock's candle and the numbers around it. For every intraday
        timeframe it is aggregated from minutes already held in memory, so
        switching tabs costs nothing at the broker. Daily candles are the one
        exception and come from the broker's own series, cached.
        """

        spec = TIMEFRAME_BY_VALUE.get(interval)
        if spec is None:
            raise TimeframeError(interval)
        now = self._clock()
        rows: list[dict[str, Any]] = []
        unavailable: list[str] = []
        for symbol in self._book.symbols():
            bars = self._board_bars(symbol, spec, now=now)
            if not bars:
                unavailable.append(symbol)
                continue
            rows.append(_board_row(symbol, bars, self._meta(symbol), now=now))

        indicator_status = (
            self._indicator_status() if self._indicator_status is not None else {}
        )
        for row in rows:
            payload = (
                None
                if self._indicators is None
                else self._indicators(str(row["symbol"]), spec.value)
            )
            row["indicators"] = None if payload is None else payload.get("values")
            row["indicator_bar_open_time"] = (
                None if payload is None else payload.get("bar_open_time")
            )

        advancing = sum(1 for row in rows if (row["change_percent"] or 0) > 0)
        declining = sum(1 for row in rows if (row["change_percent"] or 0) < 0)
        return {
            "interval": spec.value,
            "interval_label": spec.label,
            "interval_minutes": spec.minutes,
            "derived": spec.derived,
            "source": "broker" if spec.value == "1d" else "book",
            "alignment": "buckets align to the 09:15 IST session open",
            "as_of": now.isoformat(),
            "count": len(rows),
            "unavailable": unavailable,
            "summary": {
                "advancing": advancing,
                "declining": declining,
                "unchanged": len(rows) - advancing - declining,
                "volume": _volume(sum(
                    (Decimal(str(row["volume"] or 0)) for row in rows), Decimal("0")
                )),
                "forming": sum(1 for row in rows if row["is_forming"]),
                "built_from_ticks": sum(1 for row in rows if row["source"] == "live"),
            },
            "market": _market_line(now),
            "indicator_version": indicator_status.get("version"),
            "indicator_stale_symbols": indicator_status.get("stale_symbols", 0),
            "rows": rows,
        }

    def _board_bars(
        self, symbol: str, spec: TimeframeSpec, *, now: datetime
    ) -> list[Bar]:
        if spec.value != "1d":
            # The board is a read path: it never pays for a rebuild.
            return self._bars(symbol, spec, now=now, refresh_stale=False)
        token = self._resolve(symbol)
        if not token:
            return []
        candles = self._day_candles(
            symbol, token, now=now, interactive=True
        )
        return aggregate(candles, spec, symbol=symbol, now=now)

    def bars(
        self,
        symbol: str,
        interval: str,
        *,
        now: datetime | None = None,
        refresh_stale: bool = True,
    ) -> list[Bar]:
        """Aggregated bars for one symbol and timeframe.

        ``refresh_stale=True`` is the background path: it rebuilds. The read
        paths pass ``False`` and serve what is cached.
        """

        spec = TIMEFRAME_BY_VALUE.get(interval)
        if spec is None:
            raise TimeframeError(interval)
        key = str(symbol).strip().upper()
        moment = now or self._clock()
        if spec.value == "1d":
            return self._board_bars(key, spec, now=moment)
        return self._bars(key, spec, now=moment, refresh_stale=refresh_stale)

    def previous_session(
        self, symbol: str, *, now: datetime | None = None, interactive: bool = False
    ) -> Candle | None:
        """The most recent completed session's daily candle, or ``None``.

        This is what pivots are built from: the previous session's high, low
        and close. Kept returning a plain candle so this module does not have
        to know about indicators at all.
        """

        key = str(symbol).strip().upper()
        token = self._resolve(key)
        if not token:
            return None
        moment = now or self._clock()
        candles = self._day_candles(key, token, now=moment, interactive=interactive)
        today = to_ist(moment).date()
        earlier = [
            candle for candle in candles if to_ist(candle.open_time).date() < today
        ]
        if not earlier:
            return None
        return max(earlier, key=lambda candle: candle.open_time)

    # -- what an agent reads ------------------------------------------------ #

    def closed_event(self, interval: str, *, grace_seconds: float = 300.0) -> dict[str, Any] | None:
        """The candle of ``interval`` that has just finished, while it is news.

        Deliberately silent about yesterday: at 09:16 the newest finished bar
        belongs to the previous session, and announcing it would wake an agent
        to act on a candle that closed hours ago.
        """

        spec = timeframe_for(interval)
        now = self._clock()
        opened, closed = last_closed_bucket(now, spec)
        age = (now - closed).total_seconds()
        if age < 0 or age > max(0.0, float(grace_seconds)):
            return None
        return {
            "interval": spec.value,
            "bar_open_time": opened.isoformat(),
            "close_time": closed.isoformat(),
            "age_seconds": round(age, 1),
            "symbols": len(self._book.symbols()),
            # The session travels with the candle. Whoever wakes on this has to
            # decide whether it may act at all, and an announcement that does not
            # say is one that cannot be acted on - or worse, one that gets read
            # as a closed market because the answer was missing rather than no.
            "market": _market_line(now),
            "as_of": now.isoformat(),
        }

    def bar_status(self, interval: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Whether the bar that has just closed has finished being built.

        A caller that wakes on its own clock at the bar boundary is asking a
        moment before the answer exists: a minute is only closed into the book
        when the first tick of the next one arrives, so at 10:15:00 exactly the
        10:14 minute may still be forming. Rather than sleeping a fixed number
        of seconds and hoping, ask this and wait for a fact.

        ``ready`` counts the symbols whose bar for that bucket is finished,
        ``building`` the ones whose bar is there but still short of minutes, and
        ``waiting`` the ones the book has not caught up with at all. There is no
        single instant at which all fifty agree - a stock that has not traded
        for a minute is legitimately short - so this reports the counts and lets
        the caller decide what fraction is enough. It is a fact, not a verdict.
        """

        spec = timeframe_for(interval)
        moment = now or self._clock()
        opened, closed = last_closed_bucket(moment, spec)
        ready = building = waiting = 0
        for symbol in self._book.symbols():
            # ``refresh_stale=True`` on purpose: the whole question is whether
            # the book has moved on since the last answer, so a cached one
            # would be the one thing that cannot answer it.
            bars = self._bars(symbol, spec, now=moment, refresh_stale=True)
            held = [bar for bar in bars if bar.open_time == opened]
            if not held:
                waiting += 1
            elif held[-1].is_complete:
                ready += 1
            else:
                building += 1
        return {
            "interval": spec.value,
            "interval_minutes": spec.minutes,
            "bar_open_time": opened.isoformat(),
            "bar_close_time": closed.isoformat(),
            "as_of": moment.isoformat(),
            "symbols": ready + building + waiting,
            "ready": ready,
            "building": building,
            "waiting": waiting,
            "complete": bool(ready + building + waiting) and building == 0 and waiting == 0,
        }

    def screen(
        self,
        interval: str,
        *,
        where: Any = (),
        match: str = "all",
        rank_by: Any = None,
        direction: str = "desc",
        limit: Any = None,
        fields: Any = None,
    ) -> dict[str, Any]:
        """Filter the universe on one timeframe and hand back a shortlist.

        The whole point is the *one* candle: every row here describes the same
        bar - the last one whose period ended - so a filter such as
        ``rsi_14 < 30 and close > previous bar`` compares like with like. A
        symbol whose last finished bar is a different candle is reported in
        ``unavailable`` rather than quietly compared against the others.
        """

        # Imported here, not at module scope: the filter vocabulary is built
        # from the indicator catalogue, and the indicator module imports this
        # one for its ``Bar``. A module-level import would be a cycle.
        from module1.screening import DEFAULT_LIMIT, screen_rows

        spec = TIMEFRAME_BY_VALUE.get(interval)
        if spec is None:
            raise TimeframeError(interval)
        now = self._clock()
        opened, closed = last_closed_bucket(now, spec)
        stamp = opened.isoformat()
        rows: list[dict[str, Any]] = []
        unavailable: list[str] = []
        for symbol in self._book.symbols():
            frame = self._finished_frame(symbol, spec.value)
            bar = self._finished_bar(symbol, spec, now=now)
            if (
                frame is None
                or frame.get("bar_open_time") != stamp
                or bar is None
                or bar.open_time != opened
            ):
                unavailable.append(symbol)
                continue
            rows.append(_screen_row(symbol, bar, frame, self._meta(symbol), now=now))

        result = screen_rows(
            rows,
            where=where,
            match=match,
            rank_by=rank_by,
            direction=direction,
            limit=DEFAULT_LIMIT if limit is None else limit,
            fields=fields,
        )
        return {
            "interval": spec.value,
            "interval_label": spec.label,
            "interval_minutes": spec.minutes,
            "derived": spec.derived,
            "bar_open_time": opened.isoformat(),
            "bar_close_time": closed.isoformat(),
            "bar_open_time_ist": format_ist(opened),
            "as_of": now.isoformat(),
            "screened": len(rows) + len(unavailable),
            "matched": result["matched"],
            "unavailable": unavailable,
            "criteria": result["criteria"],
            "market": _market_line(now),
            "rows": result["rows"],
        }

    def symbol_bundle(self, symbol: str, interval: str) -> dict[str, Any]:
        """Everything worth knowing about one stock on one timeframe, in one call.

        The bar, its indicator values, the previous session's levels and the
        day's own numbers - and the bar the values belong to, so a decision
        taken from this can be checked against the exact candle afterwards.
        A stock with nothing finished to show raises rather than handing back a
        half-built candle.
        """

        spec = TIMEFRAME_BY_VALUE.get(interval)
        if spec is None:
            raise TimeframeError(interval)
        key = str(symbol).strip().upper()
        token = self._resolve(key)
        if not token:
            raise SymbolUnresolved(key)
        now = self._clock()
        frame = self._finished_frame(key, spec.value)
        bar = self._finished_bar(key, spec, now=now)
        if (
            frame is None
            or bar is None
            or frame.get("bar_open_time") != bar.open_time.isoformat()
        ):
            raise SymbolUnresolved(f"{key} has no finished {spec.value} candle yet")
        meta = self._meta(key) or {}
        return {
            "symbol": key,
            "name": meta.get("name", key),
            "industry": meta.get("industry"),
            "instrument_token": meta.get("instrument_token") or token,
            "interval": spec.value,
            "interval_label": spec.label,
            "interval_minutes": spec.minutes,
            "derived": spec.derived,
            "as_of": now.isoformat(),
            "bar": {
                **bar.describe(),
                "bar_age_seconds": round((now - bar.close_time).total_seconds(), 1),
            },
            "indicators": frame.get("values") or {},
            "indicator_version": frame.get("indicator_version"),
            "indicator_bar_open_time": frame.get("bar_open_time"),
            "groups": frame.get("groups") or {},
            "day_levels": frame.get("day_levels"),
            "nearest_level": frame.get("nearest_level"),
            "day": {
                "last_price": meta.get("last_price"),
                "previous_close": meta.get("previous_close"),
                "change_percent": meta.get("day_change_percent"),
                "low": meta.get("day_low"),
                "high": meta.get("day_high"),
                "volume": meta.get("day_volume"),
                "price_source": meta.get("price_source"),
                "price_age_seconds": meta.get("price_age_seconds"),
            },
            "coverage": self._book.coverage(key),
            "market": _market_line(now),
        }

    def _day_candles(
        self, symbol: str, token: str, *, now: datetime, interactive: bool
    ) -> list[Candle]:
        # Daily candles change once a session, so they are cached far longer
        # than a minute series is.
        ttl = max(self._cache_seconds, DAY_CACHE_SECONDS)
        with self._lock:
            entry = self._days.get(symbol)
            if entry is not None and (now - entry.fetched_at).total_seconds() <= ttl:
                return entry.candles
        candles = self._client.day_candles(
            token, sessions=DAY_SERIES_SESSIONS, now=now, interactive=interactive
        )
        with self._lock:
            self._days[symbol] = _CacheEntry(fetched_at=now, candles=candles)
            self.fetches += 1
        return candles


def _summarise(bars: Sequence[Bar]) -> dict[str, Any]:
    if not bars:
        return {
            "high": None,
            "low": None,
            "volume": None,
            "change_percent": None,
            "up": 0,
            "down": 0,
        }
    up = sum(1 for bar in bars if (bar.change_percent or 0) > 0)
    down = sum(1 for bar in bars if (bar.change_percent or 0) < 0)
    first_open = bars[0].open
    last_close = bars[-1].close
    change_percent = (
        (last_close - first_open) / first_open * 100 if first_open > 0 else None
    )
    return {
        "high": _price(max(bar.high for bar in bars)),
        "low": _price(min(bar.low for bar in bars)),
        "volume": _volume(sum((bar.volume for bar in bars), Decimal("0"))),
        "change_percent": _percent(change_percent),
        "up": up,
        "down": down,
        "from": format_ist(bars[0].open_time),
        "to": format_ist(bars[-1].close_time),
        "session_date": to_ist(bars[-1].open_time).date().isoformat(),
    }


def _board_row(
    symbol: str,
    bars: Sequence[Bar],
    meta: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """One stock's row on a timeframe board: its candle, and everything around it."""

    latest = bars[-1]
    previous = bars[-2] if len(bars) > 1 else None
    age = (now - latest.close_time).total_seconds()
    return {
        "symbol": symbol,
        "name": meta.get("name", symbol),
        "industry": meta.get("industry"),
        "instrument_token": meta.get("instrument_token"),
        "interval": latest.interval,
        "open_time": latest.open_time.isoformat(),
        "open_time_ist": format_ist(latest.open_time),
        "close_time": latest.close_time.isoformat(),
        "close_time_ist": format_ist(latest.close_time),
        "open": _price(latest.open),
        "high": _price(latest.high),
        "low": _price(latest.low),
        "close": _price(latest.close),
        "previous_close": _price(previous.close) if previous is not None else None,
        "change": _price(latest.change),
        "change_percent": _percent(latest.change_percent),
        "move_percent": _percent(latest.move_percent),
        "range_percent": _percent(latest.range_percent),
        "volume": _volume(latest.volume),
        "volume_ratio": _ratio(latest.volume_ratio),
        "is_complete": latest.is_complete,
        "is_forming": latest.is_forming,
        "is_gappy": latest.is_gappy,
        "minutes_present": latest.minutes_present,
        "minutes_expected": latest.minutes_expected,
        "bar_age_seconds": round(age, 1),
        "source": latest.source,
        "spark": [_price(bar.close) for bar in bars[-SPARK_BARS:]],
        "day_close": meta.get("last_price"),
        "day_change_percent": meta.get("day_change_percent"),
        "day_volume": meta.get("day_volume"),
        "day_low": meta.get("day_low"),
        "day_high": meta.get("day_high"),
    }


def _market_line(now: datetime) -> dict[str, Any]:
    """The session facts every timeframe board carries with it."""

    from module1.session_clock import describe as describe_session

    return describe_session(now)


def _screen_row(
    symbol: str, bar: Bar, frame: dict[str, Any], meta: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """One stock's row for a screen: a finished bar and the values computed on it.

    The indicator values are nested under ``indicators`` so the flat fields stay
    the candle's, and the filter vocabulary knows to look in both places.
    """

    return {
        "symbol": symbol,
        "name": meta.get("name", symbol),
        "industry": meta.get("industry"),
        **bar.describe(),
        "bar_age_seconds": round((now - bar.close_time).total_seconds(), 1),
        "day_close": meta.get("last_price"),
        "day_change_percent": meta.get("day_change_percent"),
        "day_volume": meta.get("day_volume"),
        "day_low": meta.get("day_low"),
        "day_high": meta.get("day_high"),
        "indicators": frame.get("values") or {},
    }


def _forming_has_ended(bars: list[Bar], now: datetime) -> bool:
    """Whether a cached bar was still forming when its bucket has since closed.

    Without this, a bar cached at 15:14 stays labelled "forming" for ever: no
    further minute arrives to move the version counter, so nothing would
    invalidate it.
    """

    if not bars:
        return False
    last = bars[-1]
    return last.is_forming and last.close_time <= now


def _price(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def _percent(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def _ratio(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def _volume(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def timeframe_catalogue() -> dict[str, Any]:
    """The timeframes this module serves, for the UI's switcher."""

    return {
        "default": "15m",
        "timeframes": [spec.describe() for spec in TIMEFRAMES],
        "default_limit": DEFAULT_LIMIT,
        "max_limit": MAX_LIMIT,
        "alignment": "buckets align to the exchange session open (09:15 IST)",
    }

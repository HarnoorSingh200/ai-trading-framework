"""The quote board Module 1 serves.

Two sources feed one table. A background worker keeps *history* fresh - the
previous session's close, and the session that has just finished - while the
websocket keeps *prices* live. The board merges them and is careful about one
thing above all: it never presents a stored price as though it were live. Every
row says where its price came from and how old it is, so a closed market, a
quiet feed and a dead feed look different rather than alike.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from module1.broker.feed import KiteFeed, Tick
from module1.broker.kite import Candle, KiteClient, KiteError
from module1.broker.session import BrokerAuthError, SessionManager
from module1.builder import CandleBook
from module1.config import Settings
from module1.instruments import InstrumentError, InstrumentMaster
from module1.session_clock import MARKET_CLOSE
from module1.session_clock import describe as describe_session
from module1.timeutil import IST, to_ist, utc_now
from module1.universe import Constituent, load_universe

logger = logging.getLogger("module1.market")

#: Where a row's price came from.
SOURCE_LIVE = "live"
SOURCE_SESSION_CLOSE = "session_close"
SOURCE_PREVIOUS_CLOSE = "previous_close"
SOURCE_NONE = "none"


@dataclass(slots=True)
class QuoteRow:
    """One symbol's row on the board."""

    symbol: str
    name: str
    industry: str
    instrument_token: str | None = None
    last_price: Decimal | None = None
    previous_close: Decimal | None = None
    change: Decimal | None = None
    change_percent: Decimal | None = None
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    volume: Decimal | None = None
    price_source: str = SOURCE_NONE
    price_at: datetime | None = None
    price_age_seconds: float | None = None
    session_date: date | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "industry": self.industry,
            "instrument_token": self.instrument_token,
            "last_price": _price(self.last_price),
            "previous_close": _price(self.previous_close),
            "change": _price(self.change),
            "change_percent": _ratio(self.change_percent),
            "day_open": _price(self.day_open),
            "day_high": _price(self.day_high),
            "day_low": _price(self.day_low),
            "volume": _price(self.volume),
            "price_source": self.price_source,
            "price_at": None if self.price_at is None else self.price_at.isoformat(),
            "price_age_seconds": (
                None if self.price_age_seconds is None else round(self.price_age_seconds, 1)
            ),
            "session_date": None if self.session_date is None else self.session_date.isoformat(),
        }


@dataclass(slots=True)
class _History:
    """The stored candles we hold for one symbol."""

    #: The close today's move is measured against. It is the session *before*
    #: the current trading day, which is why it is stored rather than derived
    #: from whichever candle happens to be last.
    baseline_close: Decimal | None = None
    #: The most recent completed close, used when there is no better price.
    last_close: Decimal | None = None
    #: Today's session candle, once the exchange has published it.
    session: Candle | None = None
    session_date: date | None = None


@dataclass(slots=True)
class _Live:
    price: Decimal
    at: datetime


@dataclass(slots=True)
class BoardStatus:
    """What the board is doing, for the status panel."""

    started_at: datetime | None = None
    universe_size: int = 0
    resolved: int = 0
    unresolved: list[str] = field(default_factory=list)
    history_ready: bool = False
    history_refreshed_at: datetime | None = None
    history_duration_seconds: float | None = None
    refreshing: bool = False
    refresh_error: str | None = None
    session_ready: bool = False
    session_error: str | None = None
    instrument_source: str | None = None
    last_error: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "started_at": None if self.started_at is None else self.started_at.isoformat(),
            "universe_size": self.universe_size,
            "resolved": self.resolved,
            "unresolved": list(self.unresolved),
            "history_ready": self.history_ready,
            "history_refreshed_at": (
                None
                if self.history_refreshed_at is None
                else self.history_refreshed_at.isoformat()
            ),
            "history_duration_seconds": self.history_duration_seconds,
            "refreshing": self.refreshing,
            "refresh_error": self.refresh_error,
            "session_ready": self.session_ready,
            "session_error": self.session_error,
            "instrument_source": self.instrument_source,
            "last_error": self.last_error,
        }


class MarketBoard:
    """Owns the universe, the history, the live feed and the merged board."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: SessionManager,
        client: KiteClient,
        universe: Sequence[Constituent] | None = None,
        master: InstrumentMaster | None = None,
        feed: KiteFeed | None = None,
        book: CandleBook | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._session = session
        self._client = client
        self._universe: tuple[Constituent, ...] = tuple(universe or load_universe())
        self._master = master or InstrumentMaster(settings.data_dir, clock=clock)
        self._feed = feed
        self._book = book if book is not None else CandleBook(clock=clock)
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.RLock()
        self._tokens: dict[str, str] = {}
        self._history: dict[str, _History] = {}
        self._live: dict[str, _Live] = {}
        self._status = BoardStatus(universe_size=len(self._universe))
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._revision = 0

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def feed(self) -> KiteFeed | None:
        return self._feed

    @property
    def book(self) -> CandleBook:
        """The live candle series the ticks are folded into."""

        return self._book

    @property
    def revision(self) -> int:
        """Bumped on every accepted tick, for the push stream to watch."""

        with self._lock:
            return self._revision

    def set_feed(self, feed: KiteFeed | None) -> None:
        """Attach the price feed.

        The feed needs the board's tick handler and the board needs the feed,
        so this is a setter rather than a constructor argument.
        """

        self._feed = feed

    def start(self) -> dict[str, Any]:
        """Resolve instruments, sign in, and start both background sources."""

        with self._lock:
            self._status.started_at = self._clock()

        self._resolve_instruments()
        self._prepare_session()

        if self._settings.feed_enabled and self._feed is not None:
            self._feed.register_symbols(
                {int(token): symbol for symbol, token in self._tokens.items()}
            )
            self._feed.subscribe(list(self._tokens.values()))
            self._feed.start()

        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._refresh_loop, name="module1-history", daemon=True
            )
            self._worker.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=10)
        if self._feed is not None:
            self._feed.stop()
        self._client.close()
        self._master.close()

    def _resolve_instruments(self) -> None:
        symbols = [item.symbol for item in self._universe]
        try:
            found, missing = self._master.resolve(symbols)
        except InstrumentError as exc:
            with self._lock:
                self._status.last_error = str(exc)
                self._status.unresolved = list(symbols)
            logger.warning("instrument resolution failed: %s", exc)
            return
        with self._lock:
            self._tokens = {
                symbol: record.instrument_token for symbol, record in found.items()
            }
            self._status.resolved = len(self._tokens)
            self._status.unresolved = missing
            self._status.instrument_source = self._master.describe().get("source")

    def _prepare_session(self) -> None:
        try:
            self._session.ensure()
        except BrokerAuthError as exc:
            with self._lock:
                self._status.session_ready = False
                self._status.session_error = str(exc)
                self._status.last_error = str(exc)
            logger.warning("broker sign-in failed: %s", exc)
            return
        with self._lock:
            self._status.session_ready = True
            self._status.session_error = None

    # -- the history worker -------------------------------------------------- #

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            if self._day_series_is_stale():
                try:
                    self.refresh_history()
                except Exception as exc:  # noqa: BLE001 - the worker outlives any failure
                    logger.warning("history refresh failed: %s", type(exc).__name__)
            self._stop.wait(max(5.0, float(self._settings.refresh_seconds)))

    def _day_series_is_stale(self) -> bool:
        """Whether the *daily* series could have changed.

        It changes exactly twice a day: when a new session's data appears, and
        once more after the close, when today's finished candle is published.
        Refetching fifty symbols every minute in between is fifty requests a
        minute spent re-downloading yesterday.
        """

        last = self._status.history_refreshed_at
        now = self._clock()
        if last is None:
            return True
        if to_ist(last).date() != to_ist(now).date():
            return True
        closed_at = datetime.combine(to_ist(now).date(), MARKET_CLOSE, tzinfo=IST)
        return now >= closed_at and last < closed_at

    def refresh_history(self, *, symbols: Iterable[str] | None = None) -> dict[str, Any]:
        """Fetch daily candles for the universe and fold them into the board."""

        with self._lock:
            if self._status.refreshing:
                return {"skipped": "a refresh is already running"}
            targets = (
                {symbol: self._tokens[symbol] for symbol in symbols if symbol in self._tokens}
                if symbols is not None
                else dict(self._tokens)
            )
            self._status.refreshing = True
        started = time.monotonic()
        failures: dict[str, str] = {}
        try:
            for symbol, token in targets.items():
                if self._stop.is_set():
                    break
                try:
                    candles = self._client.day_candles(
                        token, sessions=self._settings.history_days, now=self._clock()
                    )
                except KiteError as exc:
                    failures[symbol] = str(exc)[:200]
                    continue
                self._store_history(symbol, candles)
        finally:
            with self._lock:
                self._status.refreshing = False
                self._status.history_duration_seconds = round(time.monotonic() - started, 2)
                if len(failures) < max(1, len(targets)):
                    # A partial refresh is still a refresh: the board shows the
                    # symbols that answered and reports the ones that did not.
                    self._status.history_ready = True
                    self._status.history_refreshed_at = self._clock()
                self._status.refresh_error = (
                    None
                    if not failures
                    else f"{len(failures)} of {len(targets)} symbols failed: "
                    + ", ".join(sorted(failures)[:5])
                )
        return {
            "symbols": len(targets),
            "failed": len(failures),
            "duration_seconds": None if self._status.history_duration_seconds is None
            else self._status.history_duration_seconds,
        }

    def _store_history(self, symbol: str, candles: Sequence[Candle]) -> None:
        ordered = sorted(candles, key=lambda candle: candle.open_time)
        if not ordered:
            return
        today = to_ist(self._clock()).date()
        last = ordered[-1]
        last_day = to_ist(last.open_time).date()
        if last_day == today:
            # Today has traded, so today's candle is the reference and the move
            # is measured from the session before it.
            session: Candle | None = last
            baseline = ordered[-2].close if len(ordered) > 1 else None
        else:
            # Today has not traded yet - the market has not opened, or the
            # exchange has not published the session. The last completed close
            # is both the latest price and the baseline a live tick is measured
            # against, which is what makes an intraday change correct.
            session = None
            baseline = last.close
        history = _History(
            baseline_close=baseline,
            last_close=last.close,
            session=session,
            session_date=last_day if session is not None else None,
        )
        with self._lock:
            self._history[symbol] = history

    # -- ticks --------------------------------------------------------------- #

    def on_tick(self, tick: Tick) -> None:
        """Record one accepted price from the feed."""

        symbol = tick.symbol
        if symbol is None and self._feed is not None:
            symbol = self._feed.symbol_for(tick.instrument_token)
        if symbol is None:
            return
        # Fold it into the minute being built, and keep the latest price for
        # the board. These are different jobs: the book wants every tick, the
        # board only wants the newest.
        self._book.on_tick(tick, symbol)
        with self._lock:
            self._live[symbol] = _Live(price=tick.price, at=tick.received_at)
            self._revision += 1

    # -- reads --------------------------------------------------------------- #

    def universe(self) -> list[dict[str, Any]]:
        with self._lock:
            tokens = dict(self._tokens)
        return [
            {
                **item.describe(),
                "instrument_token": tokens.get(item.symbol),
                "resolved": item.symbol in tokens,
            }
            for item in self._universe
        ]

    def price_rows(self, symbols: Iterable[str]) -> dict[str, Any]:
        """The latest price of named stocks, with its source and how old it is.

        Module 3 watches only the stocks it holds something in, so this answers
        for an explicit list rather than for the whole board: a caller tracking
        six positions has no use for fifty rows, and paying for them on every
        tick would be the one part of this module that does not scale with what
        is actually being tracked.

        A symbol this module does not cover is named in ``unavailable`` rather
        than quietly left out. A caller that asked about a stock and got no row
        back cannot tell "flat" from "unknown", and this module never answers a
        question it was asked with silence.
        """

        now = self._clock()
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            key = str(raw).strip().upper()
            if key and key not in seen:
                seen.add(key)
                wanted.append(key)

        by_symbol = {item.symbol: item for item in self._universe}
        rows: list[dict[str, Any]] = []
        unavailable: list[str] = []
        for key in wanted:
            item = by_symbol.get(key)
            if item is None:
                unavailable.append(key)
                continue
            rows.append(self._row(item, now).describe())
        return {
            "as_of": now.isoformat(),
            "count": len(rows),
            "requested": wanted,
            "unavailable": unavailable,
            "rows": rows,
            "market": describe_session(now),
        }

    def token_for(self, symbol: str) -> str | None:
        """The instrument token for one symbol, or ``None`` if unresolved."""

        with self._lock:
            return self._tokens.get(str(symbol).strip().upper())

    def symbol_meta(self, symbol: str) -> dict[str, Any]:
        """Everything the board already knows about one symbol.

        The timeframe views need the same company name, industry and day-level
        context the board row carries, and all of it is already in memory -
        so attaching it costs nothing rather than a second lookup.
        """

        key = str(symbol).strip().upper()
        item = next((member for member in self._universe if member.symbol == key), None)
        if item is None:
            return {}
        row = self._row(item, self._clock()).describe()
        return {
            "name": item.name,
            "industry": item.industry,
            "instrument_token": row["instrument_token"],
            "last_price": row["last_price"],
            "previous_close": row["previous_close"],
            "day_change_percent": row["change_percent"],
            "day_low": row["day_low"],
            "day_high": row["day_high"],
            "day_volume": row["volume"],
            "price_source": row["price_source"],
            "price_age_seconds": row["price_age_seconds"],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            board = self._status.describe()
            feed = None if self._feed is None else self._feed.describe()
            tokens = len(self._tokens)
        return {
            "board": board,
            "feed": feed,
            "tokens": tokens,
            "session_clock": describe_session(self._clock()),
        }

    def snapshot(self) -> dict[str, Any]:
        """The board as the UI reads it."""

        now = self._clock()
        with self._lock:
            rows = [self._row(item, now) for item in self._universe]
            board = self._status.describe()
        return {
            "universe": "NIFTY 50",
            "count": len(rows),
            "rows": [row.describe() for row in rows],
            "board": board,
            "market": describe_session(now),
            "as_of": now.isoformat(),
        }

    def _row(self, item: Constituent, now: datetime) -> QuoteRow:
        token = self._tokens.get(item.symbol)
        row = QuoteRow(symbol=item.symbol, name=item.name, industry=item.industry,
                       instrument_token=token)
        history = self._history.get(item.symbol)
        if history is not None:
            row.previous_close = history.baseline_close
            if history.session is not None:
                row.day_open = history.session.open
                row.day_high = history.session.high
                row.day_low = history.session.low
                row.volume = history.session.volume
                row.session_date = history.session_date

        live = self._live.get(item.symbol)
        age = None if live is None else (now - live.at).total_seconds()
        if live is not None and age is not None and age <= self._settings.tick_fresh_seconds:
            row.last_price = live.price
            row.price_source = SOURCE_LIVE
            row.price_at = live.at
            row.price_age_seconds = age
        elif history is not None and history.session is not None:
            row.last_price = history.session.close
            row.price_source = SOURCE_SESSION_CLOSE
            row.price_at = history.session.open_time
            # The exchange stamps a daily candle at midnight, so a seconds age
            # would be a very precise-looking number about nothing. The session
            # date is the honest answer, and the UI shows that instead.
            row.price_age_seconds = None
        elif history is not None and history.last_close is not None:
            row.last_price = history.last_close
            row.price_source = SOURCE_PREVIOUS_CLOSE
            row.price_age_seconds = None
        else:
            row.price_source = SOURCE_NONE

        if row.last_price is not None and row.previous_close:
            row.change = row.last_price - row.previous_close
            row.change_percent = (row.change / row.previous_close) * 100
        return row


def _price(value: Decimal | None) -> float | None:
    """Serialise a price for the UI, rounded where it is displayed."""

    return None if value is None else float(round(value, 2))


def _ratio(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))

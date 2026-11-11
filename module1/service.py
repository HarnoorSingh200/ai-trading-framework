"""Module 1's HTTP surface.

The UI is a read-mostly consumer: it never touches the broker, the session
cache or the instrument dump, it asks this service. Every response is a
snapshot with the moment it was taken, so the UI can show the age of what it
displays instead of implying that everything is live.

Two writes exist, both operator escape hatches rather than parts of the normal
flow: force a history refresh, and force a fresh broker sign-in. The sign-in
itself is automatic - no endpoint and no person is required for it to happen.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from common.holidays import HolidayFetchError, HolidayList
from common.holidays import refresh as refresh_holidays
from common.telegram import Notifier, NullNotifier, escape, notifier_from_env
from module1.analytics import IndicatorService
from module1.benchmark import BenchmarkError, BenchmarkService
from module1.broker.feed import KiteFeed
from module1.broker.kite import KiteClient, KiteError
from module1.broker.session import BrokerAuthError, SessionManager
from module1.broker.token_cache import TokenCache
from module1.builder import CandleBook
from module1.candles import (
    CandleService,
    SymbolUnresolved,
    TimeframeError,
    timeframe_catalogue,
    timeframe_for,
)
from module1.config import PROJECT_ROOT, Settings, load_settings
from module1.instruments import InstrumentMaster
from module1.market import MarketBoard
from module1.screening import ScreenError
from module1.screening import catalogue as screening_catalogue
from module1.secrets import ZERODHA_USER_ID, SecretStore
from module1.session_clock import default_calendar, install_calendar
from module1.session_clock import describe as describe_session
from module1.timeutil import IST, format_ist, utc_now
from module1.universe import NIFTY50_FETCHED, NIFTY50_SOURCE_URL, UNIVERSE_NAME

logger = logging.getLogger("module1.service")

#: Fastest the push stream will send a board update. A tick storm should not
#: become a browser repaint storm.
STREAM_MIN_INTERVAL_SECONDS = 0.25
STREAM_HEARTBEAT_SECONDS = 15.0


@dataclass(slots=True)
class Module1Runtime:
    """Everything Module 1 holds for the life of the process."""

    settings: Settings
    secrets: SecretStore
    cache: TokenCache
    session: SessionManager
    client: KiteClient
    master: InstrumentMaster
    board: MarketBoard
    candles: CandleService
    indicators: IndicatorService
    #: The index the agents are compared against. None means no benchmark.
    benchmark: BenchmarkService | None = None
    feed: KiteFeed | None = None
    #: Where the operator hears about a problem. Silent unless configured.
    notifier: Notifier = field(default_factory=NullNotifier)
    #: The holiday list's files and the most recent refresh outcome. The list
    #: itself is installed into the session clock, which is what the routes and
    #: the other services read.
    holiday_list: HolidayList | None = None
    holiday_report: dict[str, Any] | None = None
    _stop: threading.Event = None  # type: ignore[assignment]
    _workers: tuple[threading.Thread, ...] = ()
    warm_report: dict[str, Any] | None = None
    repair_report: dict[str, Any] | None = None
    indicator_report: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        report = self.board.start()
        self.holiday_list = HolidayList(
            (self.settings.holidays_file, self.settings.holidays_seed)
        )
        install_calendar(self.holiday_list.current())
        self._stop = threading.Event()
        self._workers = (
            threading.Thread(target=self._warm_loop, name="module1-warm", daemon=True),
            threading.Thread(target=self._repair_loop, name="module1-repair", daemon=True),
            threading.Thread(
                target=self._indicator_loop, name="module1-indicators", daemon=True
            ),
            threading.Thread(
                target=self._holiday_loop, name="module1-holidays", daemon=True
            ),
            threading.Thread(
                target=self._monitor_loop, name="module1-monitor", daemon=True
            ),
        )
        for worker in self._workers:
            worker.start()
        self._announce_session()
        return report

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self.board.stop()
        self.session.close()
        self.notifier.close()

    # -- the operator's line out -------------------------------------------- #

    def _announce_session(self) -> None:
        """Say once, at start-up, whether the broker is actually signed in."""

        session = self.session.status()
        if session["valid"]:
            self.notifier.notify(
                f"✅ <b>Module 1</b> signed in as {escape(session.get('user_id'))} "
                f"({escape(session.get('kind'))} session, source "
                f"{escape(session.get('source'))}).",
                key="module1.session",
                dedupe_seconds=0.0,
            )
            return
        self.notifier.notify(
            "❌ <b>Module 1</b> is not signed in to the broker: "
            f"{escape(session.get('last_error') or 'no session yet')}",
            key="module1.session",
            dedupe_seconds=0.0,
        )

    def _monitor_loop(self) -> None:
        """Watch the two things that fail quietly: the session and the feed.

        A sign-in that was refused, or a websocket that stopped reconnecting,
        produces no exception anywhere - the board simply starts serving
        history as though nothing were wrong. So the transitions are watched
        and reported, once per change rather than once per poll.
        """

        stop = self._stop
        last: dict[str, str] = {}
        while not stop.is_set():
            stop.wait(15.0)
            if stop.is_set():
                break
            try:
                board = self.board.status()
                session = self.session.status()
            except Exception as exc:  # noqa: BLE001 - the watcher must not die
                logger.warning("status poll failed: %s", type(exc).__name__)
                continue
            feed = board.get("feed") or {}
            board_state = board.get("board") or {}
            current = {
                "session": "valid" if session.get("valid") else "invalid",
                "feed": str(feed.get("state") or "unknown"),
                "history": "ready" if board_state.get("history_ready") else "loading",
            }
            for key, value in current.items():
                if last.get(key) == value:
                    continue
                last[key] = value
                self._report_transition(key, value, session, feed, board_state)

    def _report_transition(
        self,
        key: str,
        value: str,
        session: dict[str, Any],
        feed: dict[str, Any],
        board_state: dict[str, Any],
    ) -> None:
        if key == "session" and value == "invalid":
            self.notifier.notify(
                "⚠️ <b>Module 1</b> lost its broker session: "
                f"{escape(session.get('last_error') or 'the broker refused it')}",
                key="module1.session_lost",
                dedupe_seconds=300.0,
            )
        elif key == "session" and value == "valid":
            self.notifier.notify(
                f"✅ <b>Module 1</b> broker session is valid again "
                f"({escape(session.get('user_id'))}).",
                key="module1.session_back",
                dedupe_seconds=0.0,
            )
        elif key == "feed":
            if value in {"reconnect_exhausted", "stopped", "disconnected"}:
                self.notifier.notify(
                    f"⚠️ <b>Module 1</b> price feed is <b>{escape(value)}</b>: "
                    f"{escape(feed.get('last_error') or 'no reason recorded')} "
                    f"({feed.get('reconnects', 0)} reconnects so far)",
                    key="module1.feed_down",
                    dedupe_seconds=300.0,
                )
            elif value == "connected":
                self.notifier.notify(
                    "✅ <b>Module 1</b> price feed connected "
                    f"({feed.get('subscribed', 0)} symbols).",
                    key="module1.feed_up",
                    dedupe_seconds=0.0,
                )
        elif key == "history" and value == "ready":
            self.notifier.notify(
                "✅ <b>Module 1</b> history is ready "
                f"({board_state.get('resolved', 0)} of "
                f"{board_state.get('universe_size', 0)} symbols resolved).",
                key="module1.history_ready",
                dedupe_seconds=0.0,
            )
        error = board_state.get("refresh_error")
        if error and key == "history":
            self.notifier.notify(
                f"⚠️ <b>Module 1</b> history refresh is incomplete: {escape(error)}",
                key="module1.history_error",
                dedupe_seconds=600.0,
            )

    # -- background work ---------------------------------------------------- #

    def _holiday_loop(self) -> None:
        """Keep the local holiday list current, quietly and rarely.

        The list changes a handful of times a year, so the check is hourly and
        the fetch only happens when the stored copy is older than
        ``FRAMEWORK_HOLIDAYS_REFRESH_SECONDS`` (or does not cover this year).
        A failed fetch is a warning, never a failure: the list on disk stays in
        force and the session clock keeps answering from it.
        """

        stop = self._stop
        listings = self.holiday_list
        if listings is None:  # pragma: no cover - start() always sets it
            return
        while not stop.is_set():
            current = listings.current()
            if current.needs_refresh(
                now=utc_now(), refresh_seconds=self.settings.holidays_refresh_seconds
            ):
                try:
                    refreshed = refresh_holidays(
                        self.settings.holidays_file, existing=current
                    )
                except HolidayFetchError as exc:
                    self.holiday_report = {
                        "error": str(exc)[:200],
                        "checked_at": format_ist(utc_now()),
                    }
                    logger.warning(
                        "the NSE holiday list could not be refreshed (%s); "
                        "keeping the local copy",
                        exc,
                    )
                    stop.wait(3600.0)
                    continue
                install_calendar(refreshed)
                self.holiday_report = {
                    "refreshed_at": refreshed.fetched_at.isoformat()
                    if refreshed.fetched_at
                    else None,
                    "entries": len(refreshed.entries),
                    "years": list(refreshed.years()),
                    "file": str(refreshed.path),
                }
                logger.info(
                    "holiday list refreshed: %d entries for %s",
                    len(refreshed.entries),
                    ", ".join(str(year) for year in refreshed.years()) or "no years",
                )
            stop.wait(1800.0)

    def _warm_loop(self) -> None:
        """Seed the live book for the universe, off the critical path.

        The board does not wait for this: day prices and live ticks are already
        flowing. This fills in the minutes every timeframe is then built from.
        """

        stop = self._stop
        for _ in range(180):
            if stop.is_set():
                return
            if self.board.status()["tokens"]:
                break
            stop.wait(1.0)
        symbols = [row["symbol"] for row in self.board.universe() if row["resolved"]]
        if not symbols:
            return
        try:
            self.warm_report = self.candles.warm(
                symbols,
                sessions=self.settings.warm_sessions,
                max_workers=4,
            )
        except Exception as exc:  # noqa: BLE001 - reported in status, never fatal
            self.warm_report = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            self.notifier.notify(
                f"❌ <b>Module 1</b> warm-up failed: {escape(self.warm_report['error'])}",
                key="module1.warm",
                dedupe_seconds=600.0,
            )
            return
        self.notifier.notify(
            "🔥 <b>Module 1</b> warm-up seeded "
            f"{self.warm_report.get('seeded', 0)} of "
            f"{self.warm_report.get('requested', 0)} symbols "
            f"({len(self.warm_report.get('failed') or [])} failed).",
            key="module1.warm_done",
            dedupe_seconds=0.0,
        )

    def _repair_loop(self) -> None:
        """Fill minutes the websocket never delivered.

        Only worth doing while the feed is live: gaps are a property of the
        stream, and a quiet closed market has nothing to fill.
        """

        stop = self._stop
        while not stop.is_set():
            stop.wait(max(30.0, float(self.settings.repair_seconds)))
            if stop.is_set():
                break
            feed = self.board.status().get("feed") or {}
            if not feed.get("connected"):
                continue
            try:
                self.repair_report = self.candles.repair(limit=8)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.repair_report = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                self.notifier.notify(
                    f"⚠️ <b>Module 1</b> gap repair failed: "
                    f"{escape(self.repair_report['error'])}",
                    key="module1.repair",
                    dedupe_seconds=600.0,
                )

    def _indicator_loop(self) -> None:
        """Keep every symbol's indicator frames current, off every request path.

        A closed candle bumps the symbol's book version, which makes its frames
        stale; this drains that work in slices so a sudden fifty-symbol boundary
        never lands in one burst.
        """

        stop = self._stop
        while not stop.is_set():
            try:
                report = self.indicators.refresh_stale(limit=8)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.indicator_report = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                self.notifier.notify(
                    "⚠️ <b>Module 1</b> indicator refresh failed: "
                    f"{escape(self.indicator_report['error'])}",
                    key="module1.indicators",
                    dedupe_seconds=600.0,
                )
                stop.wait(2.0)
                continue
            self.indicator_report = report
            if report["stale"]:
                # More to do: keep going, but yield so the tick thread is not
                # starved while a boundary's worth of symbols is caught up.
                stop.wait(0.05)
                continue
            self.indicators.clear_wake()
            self.indicators.wait(1.0)

    # -- health ------------------------------------------------------------- #

    def dependencies(self) -> dict[str, str]:
        session = self.session.status()
        board = self.board.status()
        feed = board.get("feed") or {}
        listings = self.holiday_list
        calendar = (
            listings.current() if listings is not None else default_calendar()
        )
        if calendar.problem:
            holidays_state = f"unusable: {calendar.problem}"
        elif calendar.fetched_at is not None:
            holidays_state = (
                f"{len(calendar.entries)} entries, fetched "
                f"{calendar.fetched_at.astimezone(IST).date().isoformat()}"
            )
        else:
            holidays_state = "no list loaded"
        if session["valid"]:
            session_state = f"signed in as {session['user_id']}"
        elif self.secrets.get(ZERODHA_USER_ID) is None:
            session_state = "unavailable: no credentials configured"
        else:
            session_state = "unavailable: sign-in has not succeeded"
        return {
            "broker_session": session_state,
            "holidays": holidays_state,
            "instrument_master": (
                f"{board['board']['resolved']} of {board['board']['universe_size']} "
                f"symbols resolved"
            ),
            "history": (
                "ready" if board["board"]["history_ready"] else "loading"
            ),
            "price_feed": (
                "disabled" if self.feed is None else str(feed.get("state", "unknown"))
            ),
            "candles": (
                f"{self.candles.status()['book']['built_minutes']} built live, "
                f"{self.candles.status()['book']['missing_minutes']} minutes missing"
            ),
            "indicators": (
                f"{self.indicators.describe()['frames']} frames, "
                f"{self.indicators.describe()['stale_symbols']} stale"
            ),
        }

    def healthy(self) -> bool:
        """Ready to serve, not necessarily perfect: history is the minimum."""

        return bool(self.board.status()["board"]["history_ready"]) or bool(
            self.board.status()["board"]["resolved"]
        )


def build_runtime(settings: Settings | None = None) -> Module1Runtime:
    """Assemble Module 1 from configuration."""

    resolved = settings or load_settings()
    notifier = notifier_from_env("module1")
    secrets = SecretStore(resolved.secrets_file)
    cache = TokenCache(project_root=PROJECT_ROOT)
    session = SessionManager(secrets, cache=cache)
    client = KiteClient(
        session,
        rate_limit_seconds=resolved.history_rate_limit_seconds,
    )
    master = InstrumentMaster(resolved.data_dir)

    feed: KiteFeed | None = None
    book = CandleBook()
    board = MarketBoard(
        resolved,
        session=session,
        client=client,
        master=master,
        book=book,
    )
    candles = CandleService(
        client,
        resolve_token=board.token_for,
        book=book,
        symbol_meta=board.symbol_meta,
    )
    # The indicator layer reads bars, and the bar layer asks it for values to
    # attach to a row, so they are wired to each other after both exist.
    indicators = IndicatorService(candles=candles, book=book)
    # The live frame is what the table reads; the closed frame is what an agent
    # may decide on. Both are published from the same background pass.
    candles.set_indicator_provider(
        indicators.latest, indicators.describe, indicators.closed_frame
    )
    book.set_on_closed(lambda closed: indicators.note_closed(closed.symbol))
    if resolved.feed_enabled:
        user_id = secrets.get(ZERODHA_USER_ID) or "unknown"
        feed = KiteFeed(session, user_id, on_tick=board.on_tick)
        board.set_feed(feed)
    return Module1Runtime(
        settings=resolved,
        secrets=secrets,
        cache=cache,
        session=session,
        client=client,
        master=master,
        board=board,
        candles=candles,
        indicators=indicators,
        benchmark=BenchmarkService(
            master=master,
            client=client,
            symbol=resolved.benchmark_symbol,
            days=resolved.benchmark_days,
            refresh_seconds=resolved.benchmark_refresh_seconds,
        ),
        feed=feed,
        notifier=notifier,
    )


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _symbol_list(symbols: str) -> list[str]:
    """Split a comma-separated symbol query into a stable, de-duplicated list."""

    wanted: list[str] = []
    seen: set[str] = set()
    for part in str(symbols or "").split(","):
        key = part.strip().upper()
        if key and key not in seen:
            seen.add(key)
            wanted.append(key)
    return wanted


def fail(
    code: str,
    message: str,
    *,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(
        status_code=status_code, detail={"ok": False, "error": error}
    )


def create_app(runtime: Module1Runtime | None = None) -> FastAPI:
    """Build the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state: Module1Runtime = app.state.runtime
        logger.info("module 1 starting", extra=state.settings.describe())
        state.start()
        try:
            yield
        finally:
            state.stop()

    app = FastAPI(
        title="Trading Framework v3 - Module 1 Market Data",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime or build_runtime()

    @app.get("/health")
    def health() -> dict[str, Any]:
        state: Module1Runtime = app.state.runtime
        dependencies = state.dependencies()
        return {
            "ok": state.healthy(),
            "service": "module_1_market_data",
            "checked_at": utc_now().isoformat(),
            "dependencies": dependencies,
        }

    @app.get("/v1/market/status")
    def status() -> dict[str, Any]:
        state: Module1Runtime = app.state.runtime
        return ok(
            {
                "service": "module_1_market_data",
                "version": "0.1.0",
                "checked_at": utc_now().isoformat(),
                "settings": state.settings.describe(),
                "session": state.session.status(),
                "board": state.board.status(),
                "candles": state.candles.status(),
                "warm": state.warm_report,
                "repair": state.repair_report,
                "indicator_loop": state.indicator_report,
                "notifications": state.notifier.describe(),
                "instruments": state.master.describe(),
                "broker": state.client.status(),
                "universe": {
                    "name": UNIVERSE_NAME,
                    "source": NIFTY50_SOURCE_URL,
                    "fetched": NIFTY50_FETCHED,
                    "file": str(state.settings.data_dir),
                },
            }
        )

    @app.get("/v1/market/session")
    def session() -> dict[str, Any]:
        state: Module1Runtime = app.state.runtime
        return ok(
            {
                "session": state.session.status(),
                "secrets": state.secrets.describe(),
                "cache": state.cache.describe(),
            }
        )

    @app.post("/v1/market/session/login")
    def force_login() -> dict[str, Any]:
        """Sign in again now, ignoring whatever is cached."""

        state: Module1Runtime = app.state.runtime
        try:
            state.session.ensure(force=True)
        except BrokerAuthError as exc:
            raise fail("BROKER_AUTH_FAILED", str(exc), status_code=502) from exc
        return ok({"session": state.session.status()})

    @app.post("/v1/market/session/logout")
    def logout() -> dict[str, Any]:
        """Forget the cached session. The next call signs in again."""

        state: Module1Runtime = app.state.runtime
        state.session.logout()
        return ok({"session": state.session.status()})

    @app.get("/v1/market/universe")
    def universe() -> dict[str, Any]:
        state: Module1Runtime = app.state.runtime
        rows = state.board.universe()
        return ok(
            {
                "name": UNIVERSE_NAME,
                "source": NIFTY50_SOURCE_URL,
                "fetched": NIFTY50_FETCHED,
                "count": len(rows),
                "members": rows,
            }
        )

    @app.get("/v1/market/quotes")
    def quotes() -> dict[str, Any]:
        state: Module1Runtime = app.state.runtime
        return ok(state.board.snapshot())

    @app.get("/v1/market/prices")
    def prices(symbols: str = "") -> dict[str, Any]:
        """The latest price of named stocks, with its source and how old it is.

        The board answers for the whole universe; this answers for the stocks a
        caller actually holds. Module 3 asks it once when an order arrives and
        then follows the stream below, so it never has to poll for a number
        that has not moved.
        """

        state: Module1Runtime = app.state.runtime
        wanted = _symbol_list(symbols)
        if not wanted:
            raise fail(
                "SYMBOLS_REQUIRED",
                "name at least one symbol, as ?symbols=RELIANCE,TCS",
                status_code=400,
            )
        return ok(state.board.price_rows(wanted))

    @app.get("/v1/market/prices/stream")
    def prices_stream(symbols: str = "") -> StreamingResponse:
        """Push the price of named stocks whenever one of them changes.

        Only the watched rows travel, and only when a price, its source or the
        tick behind it actually moved: a stream that re-sent an unchanged row
        on every tick would be making the caller do the discarding.
        """

        state: Module1Runtime = app.state.runtime
        wanted = _symbol_list(symbols)
        if not wanted:
            raise fail(
                "SYMBOLS_REQUIRED",
                "name at least one symbol, as ?symbols=RELIANCE,TCS",
                status_code=400,
            )

        def price_events() -> Any:
            last_signature: tuple[Any, ...] | None = None
            last_revision = -1
            last_sent = 0.0
            while True:
                now = time.monotonic()
                revision = state.board.revision
                first = last_signature is None
                if first or (
                    revision != last_revision
                    and now - last_sent >= STREAM_MIN_INTERVAL_SECONDS
                ):
                    payload = state.board.price_rows(wanted)
                    last_revision = revision
                    # The tick's own time, not how long ago it was: an age that
                    # keeps ticking is not a change worth waking a caller for.
                    signature = tuple(
                        (
                            row["symbol"],
                            row["last_price"],
                            row["price_source"],
                            row["price_at"],
                        )
                        for row in payload["rows"]
                    )
                    if signature != last_signature:
                        last_signature = signature
                        last_sent = now
                        yield (
                            f"event: prices\n"
                            f"data: {json.dumps(payload, default=str)}\n\n"
                        )
                        continue
                if now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                    last_sent = now
                    yield ": keepalive\n\n"
                time.sleep(0.15)

        return StreamingResponse(
            price_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/market/session-clock")
    def session_clock() -> dict[str, Any]:
        return ok(describe_session())

    @app.get("/v1/market/holidays")
    def holidays(year: int | None = None) -> dict[str, Any]:
        """The local NSE holiday list: what is known, and when it was fetched.

        Published so the operator (and the central process's Control tab) can
        see the list the session clock is filtering with, instead of having to
        open the JSON file.
        """

        state: Module1Runtime = app.state.runtime
        listings = getattr(state, "holiday_list", None)
        calendar = (
            listings.current() if listings is not None else default_calendar()
        )
        return ok(calendar.snapshot(year=year))

    @app.get("/v1/market/timeframes")
    def timeframes() -> dict[str, Any]:
        """Which timeframes exist, and how they are built."""

        return ok(timeframe_catalogue())

    @app.get("/v1/market/board")
    def board(interval: str = "15m") -> dict[str, Any]:
        """Every stock's latest candle on one timeframe.

        This is what the timeframe tabs read. Intraday timeframes are
        aggregated from minutes already held in memory, so switching a tab
        costs no broker calls at all.
        """

        state: Module1Runtime = app.state.runtime
        try:
            return ok(state.candles.timeframe_board(interval))
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc

    @app.get("/v1/market/fields")
    def fields(interval: str = "15m") -> dict[str, Any]:
        """What a screen may filter on, and how each field may be compared.

        Published so a model stops guessing: a filter that names a field this
        module does not have is refused, and this is how it finds out which ones
        it does have.
        """

        try:
            timeframe_for(interval)
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc
        return ok(screening_catalogue(interval))

    @app.get("/v1/market/bar-status")
    def bar_status(interval: str = "15m") -> dict[str, Any]:
        """Whether the bar that has just closed has finished being built.

        Published so an agent can wake at the bar boundary, ask whether the
        book is ready, and start work the moment it is - instead of sleeping a
        number of seconds somebody picked and hoping it was long enough.
        """

        state: Module1Runtime = app.state.runtime
        try:
            return ok(state.candles.bar_status(interval))
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc

    @app.post("/v1/market/screen")
    def screen(body: dict[str, Any]) -> dict[str, Any]:
        """Filter the universe on one timeframe and return a shortlist.

        Every row describes the same candle - the last one whose period ended -
        so two conditions in one filter can never compare different bars. A
        refusal names the field or operator at fault, because the caller is
        usually a model that can correct its own filter when it is told what was
        wrong with it.
        """

        state: Module1Runtime = app.state.runtime
        try:
            return ok(
                state.candles.screen(
                    str(body.get("interval") or "15m"),
                    where=body.get("where", ()),
                    match=str(body.get("match") or "all"),
                    rank_by=body.get("rank_by"),
                    direction=str(body.get("direction") or "desc"),
                    limit=body.get("limit"),
                    fields=body.get("fields"),
                )
            )
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc
        except ScreenError as exc:
            details = {
                key: value
                for key, value in (("field", exc.field), ("op", exc.op))
                if value
            }
            raise fail(
                "SCREEN_INVALID", str(exc), status_code=400, details=details or None
            ) from exc

    @app.get("/v1/market/symbol/{symbol}")
    def symbol(symbol: str, interval: str = "15m") -> dict[str, Any]:
        """One stock's finished candle, its values, its levels and its day.

        One call rather than four, because the four belong together: the bar and
        the indicator values it carries are the same candle, and a decision
        taken from them can be checked against it afterwards.
        """

        state: Module1Runtime = app.state.runtime
        try:
            return ok(state.candles.symbol_bundle(symbol, interval))
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc
        except SymbolUnresolved as exc:
            raise fail("SYMBOL_UNRESOLVED", str(exc), status_code=404) from exc

    @app.get("/v1/market/events")
    def events(interval: str = "15m", grace: float = 300.0) -> StreamingResponse:
        """Push a ``bar_closed`` event for one timeframe, so nobody polls for it.

        The event carries the candle that just finished, not the one now being
        built, and it is only announced while it is recent: a stream that
        reconnected at 09:16 must not be told about yesterday's last bar.
        """

        state: Module1Runtime = app.state.runtime
        try:
            timeframe_for(interval)
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc

        def bar_events() -> Any:
            announced: str | None = None
            last_sent = 0.0
            while True:
                now = time.monotonic()
                payload = state.candles.closed_event(interval, grace_seconds=grace)
                if payload is not None and payload["close_time"] != announced:
                    announced = payload["close_time"]
                    last_sent = now
                    yield f"event: bar_closed\ndata: {json.dumps(payload)}\n\n"
                elif now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                    last_sent = now
                    yield ": keepalive\n\n"
                time.sleep(2.0)

        return StreamingResponse(
            bar_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/market/indicators")
    def indicators(symbol: str, interval: str = "15m") -> dict[str, Any]:
        """One symbol's latest indicator frame, with its levels and groups."""

        state: Module1Runtime = app.state.runtime
        try:
            payload = state.indicators.frame(symbol, interval)
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc
        if payload["bar_open_time"] is None:
            raise fail(
                "INDICATORS_NOT_READY",
                f"no indicator frame for {symbol} on {interval} yet",
                status_code=404,
            )
        return ok(payload)

    @app.get("/v1/market/indicators/catalogue")
    def indicators_catalogue() -> dict[str, Any]:
        """Every field, its group and how to render it."""

        return ok(IndicatorService.catalogue())

    @app.get("/v1/market/benchmark")
    def benchmark() -> dict[str, Any]:
        """The index an agent is measured against, as daily closes.

        Read from the broker like any other market fact, and cached: a daily
        candle does not change during the day.
        """

        state: Module1Runtime = app.state.runtime
        if state.benchmark is None:
            raise fail(
                "BENCHMARK_UNAVAILABLE",
                "no benchmark index is configured",
                status_code=503,
            )
        try:
            return ok(state.benchmark.daily())
        except BenchmarkError as exc:
            raise fail(
                "BENCHMARK_UNAVAILABLE", str(exc), status_code=503
            ) from exc

    @app.get("/v1/market/candles")
    def candles(
        symbol: str,
        interval: str = "15m",
        limit: int = 60,
    ) -> dict[str, Any]:
        """One symbol's candles on one timeframe, with change and volume.

        Intraday timeframes are aggregated from one-minute candles, so every
        timeframe in a response is internally consistent.
        """

        state: Module1Runtime = app.state.runtime
        try:
            return ok(state.candles.series(symbol, interval, limit=limit))
        except TimeframeError as exc:
            raise fail(
                "TIMEFRAME_UNKNOWN", str(exc), status_code=400,
                details={"supported": exc.supported},
            ) from exc
        except SymbolUnresolved as exc:
            raise fail("SYMBOL_UNRESOLVED", str(exc), status_code=404) from exc
        except KiteError as exc:
            raise fail("BROKER_ERROR", str(exc), status_code=502) from exc

    @app.get("/v1/market/stream")
    def stream() -> StreamingResponse:
        """Push the board when something changes, instead of being polled.

        Server-sent events rather than websockets: the traffic is one-way, the
        browser reconnects on its own, and it is plain HTTP, so the UI keeps
        talking to its own origin through the same proxy.
        """

        state: Module1Runtime = app.state.runtime

        def events() -> Any:
            last_seen = -1
            last_sent = 0.0
            while True:
                revision = state.board.revision
                now = time.monotonic()
                if revision != last_seen and now - last_sent >= STREAM_MIN_INTERVAL_SECONDS:
                    last_seen = revision
                    last_sent = now
                    payload = json.dumps(state.board.snapshot(), default=str)
                    yield f"event: board\ndata: {payload}\n\n"
                elif now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                    last_sent = now
                    yield ": keepalive\n\n"
                time.sleep(0.15)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/market/refresh")
    def refresh() -> dict[str, Any]:
        state: Module1Runtime = app.state.runtime
        result = state.board.refresh_history()
        state.candles.invalidate()
        return ok({**result, "candles_invalidated": True})

    @app.post("/v1/market/repair")
    def repair(limit: int = 12) -> dict[str, Any]:
        """Run a repair pass now: fill holes, and confirm built minutes."""

        state: Module1Runtime = app.state.runtime
        return ok(state.candles.repair(limit=limit))

    @app.get("/")
    def root() -> dict[str, Any]:
        return ok(
            {
                "service": "module_1_market_data",
                "endpoints": [
                    "/health",
                    "/v1/market/status",
                    "/v1/market/session",
                    "/v1/market/universe",
                    "/v1/market/quotes",
                    "/v1/market/prices",
                    "/v1/market/prices/stream",
                    "/v1/market/session-clock",
                    "/v1/market/holidays",
                    "/v1/market/timeframes",
                    "/v1/market/board",
                    "/v1/market/fields",
                    "/v1/market/bar-status",
                    "/v1/market/screen",
                    "/v1/market/symbol/{symbol}",
                    "/v1/market/events",
                    "/v1/market/indicators",
                    "/v1/market/indicators/catalogue",
                    "/v1/market/benchmark",
                    "/v1/market/candles",
                    "/v1/market/stream",
                    "/v1/market/refresh",
                    "/v1/market/repair",
                    "/v1/market/session/login",
                    "/v1/market/session/logout",
                ],
                "exchange_time": format_ist(utc_now()),
            }
        )

    return app


app = create_app()

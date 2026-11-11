"""Module 3's HTTP surface: the order book, and the feed that watches it.

Two things live in this process, and they are deliberately in the same one.
The tracker holds the only copy of what is open and moves it as prices arrive;
the control surface lets an operator - or Module 2 - put an order in, and lets
the site watch the whole book. Splitting them would mean the state that decides
a trade lived somewhere nothing could see it.

The feed is a server-sent event stream over the ledger file rather than a queue
in memory, for the same reason Module 2's is: a reader that connects late, or
reconnects after a restart, is shown what is happening now instead of what
happened to be in a buffer when the process came up.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from common.telegram import Notifier, NullNotifier, escape, notifier_from_env
from module3.clients import DecisionsClient, MarketClient, PriceStream
from module3.config import Settings, load_settings
from module3.orders import OrderError
from module3.store import OrderStore
from module3.timeutil import utc_now
from module3.tracker import OrderTracker

logger = logging.getLogger("module3.service")

#: How often the feed looks for new lines, and the heartbeat when there are none.
TAIL_INTERVAL_SECONDS: float = 0.5
STREAM_HEARTBEAT_SECONDS: float = 15.0
#: How much of an existing ledger a newly connected reader is shown.
TAIL_BACKLOG_BYTES: int = 64 * 1024


@dataclass(slots=True)
class Module3Runtime:
    """Everything Module 3 holds for the life of the process."""

    settings: Settings
    store: OrderStore
    tracker: OrderTracker
    #: Where the operator hears about fills, closes and failures.
    notifier: Notifier = field(default_factory=NullNotifier)
    market: MarketClient | None = None
    stream: PriceStream | None = None
    directory: DecisionsClient | None = None
    price_report: dict[str, Any] | None = None
    reconcile_report: dict[str, Any] | None = None
    last_error: str | None = None
    _stop: threading.Event | None = field(default=None, repr=False)
    _threads: tuple[threading.Thread, ...] = ()

    def start(self) -> dict[str, Any]:
        self._stop = threading.Event()
        workers: list[threading.Thread] = [
            threading.Thread(target=self._clock_loop, name="module3-clock", daemon=True)
        ]
        if self.stream is not None:
            workers.append(
                threading.Thread(target=self._price_loop, name="module3-prices", daemon=True)
            )
        if self.directory is not None and self.settings.reconcile_seconds > 0:
            workers.append(
                threading.Thread(
                    target=self._reconcile_loop, name="module3-reconcile", daemon=True
                )
            )
        self._threads = tuple(workers)
        for worker in workers:
            worker.start()
        return self.status()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        for worker in self._threads:
            if worker.is_alive():
                worker.join(timeout=5)
        stats = self.tracker.stats()
        self.notifier.notify(
            "⏹ <b>Module 3</b> stopping. Book: "
            f"{stats['orders']} orders tracked, "
            f"{stats['by_status'].get('open', 0)} open, "
            f"{stats['by_status'].get('pending', 0)} pending.",
            key="module3.stopped",
            dedupe_seconds=0.0,
        )
        self.notifier.flush(timeout=3.0)
        self.notifier.close()
        for component in (self.stream, self.market, self.directory):
            if component is not None:
                component.close()

    # -- background work ----------------------------------------------------- #

    def _clock_loop(self) -> None:
        """Run the two clocks: the holding deadline, and the price safety net.

        They are one loop on purpose. Both exist for the same case - a stock
        that has gone quiet - and two loops would be two things to reason about
        when the answer is one: nothing came in, so go and ask.
        """

        stop = self._stop
        if stop is None:  # pragma: no cover - start() always sets it
            return
        interval = max(0.05, float(self.settings.sweep_seconds))
        poll = float(self.settings.poll_seconds)
        last_poll = 0.0
        while not stop.is_set():
            try:
                self.tracker.sweep()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.last_error = f"clock: {type(exc).__name__}: {exc}"[:200]
                self.notifier.notify(
                    f"⚠️ <b>Module 3</b> clock sweep failed: {escape(self.last_error)}",
                    key="module3.clock",
                    dedupe_seconds=300.0,
                )
            now = time.monotonic()
            if self.market is not None and poll > 0 and now - last_poll >= poll:
                last_poll = now
                try:
                    self.price_report = self.tracker.refresh_prices()
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    self.price_report = {
                        "error": f"{type(exc).__name__}: {exc}"[:200]
                    }
                    self.notifier.notify(
                        "⚠️ <b>Module 3</b> price poll failed: "
                        f"{escape(self.price_report['error'])}",
                        key="module3.price_poll",
                        dedupe_seconds=300.0,
                    )
            stop.wait(interval)

    def _price_loop(self) -> None:
        stream = self.stream
        stop = self._stop
        if stream is None or stop is None:  # pragma: no cover - gated by start()
            return
        try:
            stream.run(stop)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.last_error = f"price stream: {type(exc).__name__}: {exc}"[:200]
            self.notifier.notify(
                f"⚠️ <b>Module 3</b> price stream stopped: {escape(self.last_error)}",
                key="module3.price_stream",
                dedupe_seconds=300.0,
            )

    def _reconcile_loop(self) -> None:
        directory = self.directory
        stop = self._stop
        if directory is None or stop is None:  # pragma: no cover - gated by start()
            return
        interval = max(1.0, float(self.settings.reconcile_seconds))
        while not stop.is_set():
            try:
                self.reconcile_report = self.tracker.reconcile(
                    directory,
                    max_age_minutes=self.settings.reconcile_max_age_minutes,
                )
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.reconcile_report = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                self.notifier.notify(
                    "⚠️ <b>Module 3</b> reconciliation failed: "
                    f"{escape(self.reconcile_report['error'])}",
                    key="module3.reconcile",
                    dedupe_seconds=300.0,
                )
            stop.wait(interval)

    # -- status -------------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        return {
            "service": "module_3_order_manager",
            "version": "0.1.0",
            "checked_at": utc_now().isoformat(),
            "settings": self.settings.describe(),
            "orders": self.tracker.stats(),
            "prices": self.tracker.prices(),
            "price_stream": None if self.stream is None else self.stream.status(),
            "price_poll": self.price_report,
            "reconcile": self.reconcile_report,
            "notifications": self.notifier.describe(),
            "last_error": self.last_error,
            "broker": (
                "not connected, by design: this module tracks positions and "
                "cannot place an order"
            ),
        }


def build_runtime(settings: Settings | None = None) -> Module3Runtime:
    """Assemble Module 3 from configuration."""

    resolved = settings or load_settings()
    notifier = notifier_from_env("module3")
    store = OrderStore(resolved.data_dir)
    market = MarketClient(
        resolved.module1_url, timeout_seconds=resolved.http_timeout_seconds
    )
    tracker = OrderTracker(
        store=store,
        market=market,
        notifier=notifier,
        window=resolved.window,
        max_fill_drift_pct=resolved.max_fill_drift_pct,
        reconcile_max_age_minutes=resolved.reconcile_max_age_minutes,
        stale_price_seconds=resolved.stale_price_seconds,
    )
    # The stream is told what to watch rather than being subscribed to the whole
    # universe: what this module holds is the only thing it can act on, and
    # asking for anything else would be paying for a tape it cannot use.
    stream = PriceStream(
        resolved.module1_url,
        symbols=tracker.watch_symbols,
        on_prices=tracker.on_prices,
    )
    directory = DecisionsClient(
        resolved.module2_url, timeout_seconds=resolved.http_timeout_seconds
    )
    return Module3Runtime(
        settings=resolved,
        store=store,
        tracker=tracker,
        notifier=notifier,
        market=market,
        stream=stream,
        directory=directory,
    )


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


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
    return HTTPException(status_code=status_code, detail={"ok": False, "error": error})


def create_app(runtime: Module3Runtime | None = None) -> FastAPI:
    """Build the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        state: Module3Runtime = app.state.runtime
        logger.info("module 3 starting", extra=state.settings.describe())
        state.start()
        try:
            yield
        finally:
            state.stop()

    app = FastAPI(
        title="Trading Framework v3 - Module 3 Order Manager",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime or build_runtime()

    @app.get("/health")
    def health() -> dict[str, Any]:
        state: Module3Runtime = app.state.runtime
        stats = state.tracker.stats()
        return {
            "ok": True,
            "service": "module_3_order_manager",
            "checked_at": utc_now().isoformat(),
            "orders": {
                "tracked": stats["orders"],
                "open": stats["by_status"].get("open", 0),
                "pending": stats["by_status"].get("pending", 0),
            },
            "price_stream": None if state.stream is None else state.stream.status()["state"],
        }

    @app.post("/v1/orders")
    def submit(body: dict[str, Any]) -> dict[str, Any]:
        """Take an order from Module 2.

        The body is the decision record Module 2 already keeps, so the two
        modules share one description of a trade instead of two that drift. A
        decision to stand aside is acknowledged and not stored: it is a fact
        about an agent, and it belongs in the log where the agent's reasoning
        lives.
        """

        state: Module3Runtime = app.state.runtime
        try:
            result = state.tracker.ingest(body)
        except OrderError as exc:
            details = {"field": exc.field} if exc.field else None
            raise fail("ORDER_INVALID", str(exc), status_code=400, details=details) from exc
        return ok(result)

    @app.get("/v1/orders")
    def list_orders(
        status: str | None = None,
        agent_id: str | None = None,
        symbol: str | None = None,
        day: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        state: Module3Runtime = app.state.runtime
        rows = state.tracker.view()
        if status:
            rows = [row for row in rows if row["status"] == status]
        if agent_id:
            rows = [row for row in rows if row["agent_id"] == agent_id]
        if day:
            rows = [row for row in rows if row["session_date"] == day]
        if symbol:
            wanted = symbol.strip().upper()
            rows = [row for row in rows if row["symbol"] == wanted]
        bounded = max(1, min(int(limit), 1000))
        return ok(
            {
                "count": len(rows[:bounded]),
                "orders": rows[:bounded],
                "stats": state.tracker.stats(),
            }
        )

    # A literal path, registered before ``/{order_id}``: the other way round,
    # "stream" would be read as an order id and answered with a 404.
    @app.get("/v1/orders/stream")
    def stream_orders() -> StreamingResponse:
        state: Module3Runtime = app.state.runtime
        return StreamingResponse(
            tail_events(state.store),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # The daily book: what happened today, what happened on any other day, and
    # the few numbers a caller is usually adding up by hand.
    @app.get("/v1/orders/days")
    def order_days() -> dict[str, Any]:
        """Every exchange day the book holds anything for, newest first."""

        state: Module3Runtime = app.state.runtime
        days = state.tracker.days()
        return ok({"count": len(days), "days": days})

    @app.get("/v1/orders/summary")
    def order_summary(agent_id: str | None = None, day: str | None = None) -> dict[str, Any]:
        """How a period went: open, closed, what it made, and what is open now."""

        state: Module3Runtime = app.state.runtime
        return ok(state.tracker.summary(agent_id=agent_id, day=day))

    @app.post("/v1/orders/prune")
    def prune_orders(before: str) -> dict[str, Any]:
        """Forget finished orders taken before a day. Pass tomorrow to clear.

        This is the one write that destroys anything, so it says what it will do
        before it does it: the answer names what was dropped, what was kept, and
        anything it refused to touch. It cannot check whether Module 4 has
        archived these yet - that is the caller's business - so a caller that
        cares should sync Module 4 first.
        """

        state: Module3Runtime = app.state.runtime
        try:
            moment = datetime.fromisoformat(before.strip())
        except ValueError as exc:
            raise fail(
                "PRUNE_INVALID",
                f"before must be a date like 2026-09-30, not {before!r}",
                status_code=400,
            ) from exc
        return ok(state.tracker.prune(before=moment.date().isoformat()))

    @app.post("/v1/orders/reconcile")
    def reconcile_now() -> dict[str, Any]:
        """Ask Module 2 what it decided, now, rather than on the timer."""

        state: Module3Runtime = app.state.runtime
        if state.directory is None:
            raise fail(
                "MODULE2_UNAVAILABLE",
                "no directory for Module 2 is configured",
                status_code=503,
            )
        report = state.tracker.reconcile(
            state.directory, max_age_minutes=state.settings.reconcile_max_age_minutes
        )
        state.reconcile_report = report
        return ok(report)

    @app.get("/v1/orders/{order_id}")
    def get_order(order_id: str) -> dict[str, Any]:
        state: Module3Runtime = app.state.runtime
        order = state.tracker.order(order_id)
        if order is None:
            raise fail(
                "ORDER_NOT_FOUND",
                f"no order with id {order_id!r} is on the ledger",
                status_code=404,
            )
        rows = [row for row in state.tracker.view() if row["order_id"] == order_id]
        return ok({"order": rows[0] if rows else order.describe()})

    @app.get("/v1/orders/{order_id}/events")
    def order_events(order_id: str) -> dict[str, Any]:
        """Everything that happened to one order, oldest first."""

        state: Module3Runtime = app.state.runtime
        if state.tracker.order(order_id) is None:
            raise fail(
                "ORDER_NOT_FOUND",
                f"no order with id {order_id!r} is on the ledger",
                status_code=404,
            )
        events = [
            event
            for event in state.store.read_events(limit=None)
            if event.get("order_id") == order_id
        ]
        return ok({"order_id": order_id, "count": len(events), "events": events})

    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        """What this module is doing, including the price subscription."""

        state: Module3Runtime = app.state.runtime
        return ok(state.status())

    @app.get("/")
    def root() -> dict[str, Any]:
        return ok(
            {
                "service": "module_3_order_manager",
                "endpoints": [
                    "/health",
                    "/v1/orders",
                    "/v1/orders/stream",
                    "/v1/orders/days",
                    "/v1/orders/summary",
                    "/v1/orders/prune",
                    "/v1/orders/reconcile",
                    "/v1/orders/{order_id}",
                    "/v1/orders/{order_id}/events",
                    "/v1/status",
                ],
                "exchange_time": utc_now().isoformat(),
            }
        )

    return app


# -- the feed ---------------------------------------------------------------- #


@dataclass
class _Tail:
    """The ledger file, and how far it has been read."""

    path: Path
    offset: int = 0
    buffer: str = ""
    seen: bool = False


def _tail_once(tail: _Tail) -> list[str]:
    """New whole lines since last time. A half-written line waits for its end."""

    if not tail.path.exists():
        return []
    try:
        size = tail.path.stat().st_size
        if not tail.seen:
            # First look at an existing ledger: start near the end rather than
            # at the beginning, or connecting would replay the whole session.
            tail.seen = True
            tail.offset = max(0, size - TAIL_BACKLOG_BYTES)
        if size < tail.offset:
            # The file was replaced or truncated under us; start again.
            tail.offset, tail.buffer = 0, ""
        with tail.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(tail.offset)
            chunk = handle.read()
            tail.offset = handle.tell()
    except OSError:
        return []
    if not chunk:
        return []
    tail.buffer += chunk
    lines = tail.buffer.split("\n")
    tail.buffer = lines.pop()
    return [line for line in lines if line.strip()]


def tail_events(
    store: OrderStore,
    *,
    interval: float = TAIL_INTERVAL_SECONDS,
    stop: threading.Event | None = None,
) -> Iterator[str]:
    """Yield server-sent events for the order ledger as it is written."""

    tail = _Tail(path=store.events_path)
    last_sent = time.monotonic()
    while stop is None or not stop.is_set():
        wrote = False
        for line in _tail_once(tail):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            wrote = True
            last_sent = time.monotonic()
            yield f"event: order\ndata: {json.dumps(payload, default=str)}\n\n"
        now = time.monotonic()
        if not wrote and now - last_sent >= STREAM_HEARTBEAT_SECONDS:
            last_sent = now
            yield ": keepalive\n\n"
        time.sleep(interval)


app = create_app()

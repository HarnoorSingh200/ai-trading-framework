"""Module 4's HTTP surface: the long record, and what it says.

This is a dashboard's backend. It answers questions about every order the
framework has ever taken - across days and across agents - which is the one
thing Module 3's book of the day deliberately does not.

A background thread keeps the archive up to date from Module 3, and the
endpoints only ever read. Nothing here can change an order, and nothing here
can place one.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException

from common.telegram import Notifier, NullNotifier, escape, notifier_from_env
from module4.benchmark import Benchmark, BenchmarkStore
from module4.clients import AgentUsageClient, BenchmarkClient, OrderBookClient
from module4.config import (
    MAX_ORDER_LIMIT,
    Settings,
    load_settings,
)
from module4.ledger import Ledger
from module4.s3 import S3Archive
from module4.store import HistoryStore
from module4.timeutil import utc_now
from module4.usage import TokenUsage, UsageStore

logger = logging.getLogger("module4.service")


@dataclass(slots=True)
class Module4Runtime:
    """Everything Module 4 holds for the life of the process."""

    settings: Settings
    store: HistoryStore
    ledger: Ledger
    #: Where the operator hears about a sync that stopped working.
    notifier: Notifier = field(default_factory=NullNotifier)
    book: OrderBookClient | None = None
    #: The agent engine's token totals, mirrored through its HTTP route.
    usage: TokenUsage | None = None
    usage_client: AgentUsageClient | None = None
    #: The index the agents are measured against, mirrored from Module 1.
    benchmark: Benchmark | None = None
    benchmark_client: BenchmarkClient | None = None
    last_error: str | None = None
    _stop: threading.Event | None = field(default=None, repr=False)
    _threads: tuple[threading.Thread, ...] = ()

    def start(self) -> dict[str, Any]:
        self._stop = threading.Event()
        self.ledger.load_remote()
        threads: list[threading.Thread] = []
        if self.settings.sync_seconds > 0:
            if self.book is not None:
                threads.append(
                    threading.Thread(
                        target=self._sync_loop, name="module4-sync", daemon=True
                    )
                )
            if self.usage is not None and self.usage_client is not None:
                threads.append(
                    threading.Thread(
                        target=self._usage_loop, name="module4-usage", daemon=True
                    )
                )
            if self.benchmark is not None and self.benchmark_client is not None:
                threads.append(
                    threading.Thread(
                        target=self._benchmark_loop,
                        name="module4-benchmark",
                        daemon=True,
                    )
                )
        self._threads = tuple(threads)
        for worker in self._threads:
            worker.start()
        return self.status()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        for worker in self._threads:
            if worker.is_alive():
                worker.join(timeout=5)
        remote = self.ledger.publish_remote(force=True)
        if remote.get("error"):
            self.last_error = str(remote["error"])
        archive = self.ledger.status()
        self.notifier.notify(
            "⏹ <b>Module 4</b> stopping. Archive: "
            f"{archive['orders']} orders across {archive['days']} days.",
            key="module4.stopped",
            dedupe_seconds=0.0,
        )
        self.notifier.flush(timeout=3.0)
        self.notifier.close()
        if self.book is not None:
            self.book.close()
        if self.usage_client is not None:
            self.usage_client.close()
        if self.benchmark_client is not None:
            self.benchmark_client.close()

    def _sync_loop(self) -> None:
        stop = self._stop
        if stop is None:  # pragma: no cover - start() always sets it
            return
        interval = max(1.0, float(self.settings.sync_seconds))
        while not stop.is_set():
            try:
                report = self.ledger.sync()
                remote = report.get("s3") or {}
                if remote.get("error"):
                    self.last_error = str(remote["error"])
                    self.notifier.notify(
                        f"⚠️ <b>Module 4</b> S3 archive failed: "
                        f"{escape(self.last_error)}",
                        key="module4.s3",
                        dedupe_seconds=600.0,
                    )
                else:
                    # The archive is well: clear a failure the operator may
                    # still be looking at from earlier in the day.
                    self.last_error = None
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.last_error = f"sync: {type(exc).__name__}: {exc}"[:200]
                self.notifier.notify(
                    f"⚠️ <b>Module 4</b> sync failed: {escape(self.last_error)}",
                    key="module4.sync",
                    dedupe_seconds=600.0,
                )
            stop.wait(interval)

    def _usage_loop(self) -> None:
        """Keep the token record current from Module 2, while it is running.

        Outside the trading day Module 2 is resting and this read fails; that
        is not an error worth telling anyone about, and what was already
        recorded is left untouched.
        """

        stop = self._stop
        if stop is None:  # pragma: no cover - start() always sets it
            return
        interval = max(1.0, float(self.settings.sync_seconds))
        usage = self.usage
        if usage is None:  # pragma: no cover - start() only starts it with one
            return
        while not stop.is_set():
            # A failure here is normal while Module 2 is resting outside the
            # trading day. It belongs in the usage status, never in the
            # archive's last_error: the Reporting tab would otherwise call a
            # closed market a broken archive.
            with suppress(Exception):
                usage.sync()
            stop.wait(interval)

    def _benchmark_loop(self) -> None:
        """Keep the benchmark closes current from Module 1, while it is up."""

        stop = self._stop
        if stop is None:  # pragma: no cover - start() always sets it
            return
        interval = max(1.0, float(self.settings.sync_seconds))
        benchmark = self.benchmark
        if benchmark is None:  # pragma: no cover - start() only starts it with one
            return
        while not stop.is_set():
            # Module 1 may be starting (it and this module boot together) or
            # between sessions; that is not an archive fault.
            with suppress(Exception):
                benchmark.sync()
            stop.wait(interval)

    def status(self) -> dict[str, Any]:
        return {
            "service": "module_4_reporting",
            "version": "0.1.0",
            "checked_at": utc_now().isoformat(),
            "settings": self.settings.describe(),
            "archive": self.ledger.status(),
            "usage": None if self.usage is None else self.usage.status(),
            "benchmark": (
                None if self.benchmark is None else self.benchmark.status()
            ),
            "notifications": self.notifier.describe(),
            "last_error": self.last_error,
            "broker": (
                "not connected, by design: this module reads an order book and "
                "cannot place an order"
            ),
        }


def build_runtime(settings: Settings | None = None) -> Module4Runtime:
    """Assemble Module 4 from configuration."""

    resolved = settings or load_settings()
    notifier = notifier_from_env("module4")
    store = HistoryStore(resolved.data_dir)
    remote = (
        S3Archive(
            resolved.s3_bucket,
            prefix=resolved.s3_prefix,
            region=resolved.s3_region,
        )
        if resolved.s3_enabled
        else None
    )
    book = OrderBookClient(
        resolved.module3_url, timeout_seconds=resolved.http_timeout_seconds
    )
    ledger = Ledger(store=store, book=book, remote=remote)
    usage_client = AgentUsageClient(
        resolved.module2_url, timeout_seconds=resolved.http_timeout_seconds
    )
    benchmark_client = BenchmarkClient(
        resolved.module1_url, timeout_seconds=resolved.http_timeout_seconds
    )
    return Module4Runtime(
        settings=resolved,
        store=store,
        ledger=ledger,
        notifier=notifier,
        book=book,
        usage=TokenUsage(store=UsageStore(resolved.data_dir), client=usage_client),
        usage_client=usage_client,
        benchmark=Benchmark(
            store=BenchmarkStore(resolved.data_dir), client=benchmark_client
        ),
        benchmark_client=benchmark_client,
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


def create_app(runtime: Module4Runtime | None = None) -> FastAPI:
    """Build the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        state: Module4Runtime = app.state.runtime
        logger.info("module 4 starting", extra=state.settings.describe())
        state.start()
        try:
            yield
        finally:
            state.stop()

    app = FastAPI(
        title="Trading Framework v3 - Module 4 Reporting",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime or build_runtime()

    @app.get("/health")
    def health() -> dict[str, Any]:
        state: Module4Runtime = app.state.runtime
        archive = state.ledger.status()
        return {
            "ok": True,
            "service": "module_4_reporting",
            "checked_at": utc_now().isoformat(),
            "archive": {"orders": archive["orders"], "days": archive["days"]},
            "last_sync": archive["synced_at"],
            "usage": None if state.usage is None else state.usage.status(),
            "benchmark": (
                None if state.benchmark is None else state.benchmark.status()
            ),
        }

    @app.get("/v1/performance")
    def performance(agent_id: str | None = None, day: str | None = None) -> dict[str, Any]:
        """How it has gone, overall or for one agent, one day or all of them."""

        state: Module4Runtime = app.state.runtime
        return ok(state.ledger.performance(agent_id=agent_id, day=day))

    @app.get("/v1/analytics")
    def analytics(agent_id: str | None = None, day: str | None = None) -> dict[str, Any]:
        """The dashboard's charts and the numbers behind them."""

        state: Module4Runtime = app.state.runtime
        return ok(state.ledger.analytics(agent_id=agent_id, day=day))

    @app.get("/v1/days")
    def days(agent_id: str | None = None) -> dict[str, Any]:
        """One row per exchange day, newest first."""

        state: Module4Runtime = app.state.runtime
        rows = state.ledger.days(agent_id=agent_id)
        return ok({"count": len(rows), "days": rows})

    @app.get("/v1/agents")
    def agents() -> dict[str, Any]:
        """One row per agent, best first by what it has actually made."""

        state: Module4Runtime = app.state.runtime
        rows = state.ledger.agents()
        return ok({"count": len(rows), "agents": rows})

    @app.get("/v1/usage")
    def token_usage(day: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
        """What the AI agents spent: tokens and cycle timing per day and agent.

        The totals are mirrored from Module 2 while it is running, so the
        dashboard still answers after the agent engine has gone to rest. A
        per-cycle row carries the work duration the agent process measured, not
        the sleep between two candles.
        """

        state: Module4Runtime = app.state.runtime
        if state.usage is None:
            return ok({"count": 0, "days": [], "tracked_days": 0, "updated_at": None})
        return ok(state.usage.report(day=day, agent_id=agent_id))

    @app.get("/v1/benchmark")
    def benchmark() -> dict[str, Any]:
        """The index the agents are measured against, as daily closes.

        Kept here so the dashboard can compare an agent's realised P&L with a
        buy-and-hold of NIFTY 50 over the same days.
        """

        state: Module4Runtime = app.state.runtime
        if state.benchmark is None:
            return ok({"symbol": None, "candles": [], "notional": state.settings.benchmark_notional})
        return ok(
            {
                **state.benchmark.report(),
                "notional": state.settings.benchmark_notional,
            }
        )

    @app.post("/v1/usage/sync")
    def sync_token_usage() -> dict[str, Any]:
        """Ask Module 2 for its totals now rather than on the timer."""

        state: Module4Runtime = app.state.runtime
        if state.usage is None:
            raise fail(
                "MODULE2_UNAVAILABLE",
                "no agent engine is configured",
                status_code=503,
            )
        return ok(state.usage.sync())

    @app.get("/v1/orders")
    def orders(
        agent_id: str | None = None,
        day: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """The long book: every order archived, newest first."""

        state: Module4Runtime = app.state.runtime
        rows = state.ledger.orders(
            agent_id=agent_id,
            day=day,
            status=status,
            symbol=symbol,
            limit=max(1, min(int(limit), MAX_ORDER_LIMIT)),
        )
        return ok({"count": len(rows), "orders": rows})

    @app.post("/v1/sync")
    def sync_now() -> dict[str, Any]:
        """Read Module 3's book now rather than on the timer."""

        state: Module4Runtime = app.state.runtime
        if state.book is None:
            raise fail(
                "MODULE3_UNAVAILABLE",
                "no order book is configured",
                status_code=503,
            )
        report = state.ledger.sync(force_remote=True)
        if state.usage is not None:
            # The order archive is the day's record; the token totals are the
            # day's bill. A caller doing a final sync gets both.
            report["usage"] = state.usage.sync()
        if state.benchmark is not None:
            report["benchmark"] = state.benchmark.sync()
        return ok(report)

    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        state: Module4Runtime = app.state.runtime
        return ok(state.status())

    @app.get("/")
    def root() -> dict[str, Any]:
        return ok(
            {
                "service": "module_4_reporting",
                "endpoints": [
                    "/health",
                    "/v1/analytics",
                    "/v1/performance",
                    "/v1/days",
                    "/v1/agents",
                    "/v1/usage",
                    "/v1/usage/sync",
                    "/v1/benchmark",
                    "/v1/orders",
                    "/v1/sync",
                    "/v1/status",
                ],
                "exchange_time": utc_now().isoformat(),
            }
        )

    return app


app = create_app()

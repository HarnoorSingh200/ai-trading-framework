"""Run the whole framework as one central process: ``python -m runner``.

    python -m runner            run the framework: market data and reporting first,
                                then the trading day
    python -m runner --check    validate configuration, send a test Telegram
    python -m runner --any-time ignore the trading window (development only)

This is the entry point a cloud machine runs (systemd, a scheduled task, a
container) and the one the operator double-clicks. It holds no market data and
makes no trading decision; it decides *when* the four services exist, restarts
one that dies, keeps the framework readable after the close, and tells the
operator what happened.

The day it keeps:

* **Module 1 always.** Market data fetches history, builds candles and computes
  indicators whether or not the exchange is open, and it is given a minute to
  stabilise - the session, the resolved universe, the history, the warm-up pass
  and the indicators all have to say they are ready - before anything depends
  on it. A fast answer is not mistaken for a stable one.
* **Module 4 always, started alongside Module 1.** The archive keeps the
  reports even when the market is closed, and it does not depend on market data
  being stable: starting it with Module 1 means the first reports exist before
  the market warms up, and a slow Module 1 never delays the archive.
* **Modules 3 and 2 for the trading day**, started a few minutes before the
  decision window and stopped when their work can no longer be due - Module 2
  after the last cycle, Module 3 when the day is flat and its book has been
  archived. Outside that window they are *resting*, and the site says why: the
  market is not open, so there is nothing to show there.

The central process does not exit at the close, and it does not refuse to start
outside the window: the operator's machine keeps serving last available data
and the long record until it is stopped. Its own control surface
(``FRAMEWORK_CONTROL_PORT``, 5205 by default) is what the Control tab reads:
the status of every service, its logs, and a restart button.

When ``FRAMEWORK_SELF_SHUTDOWN`` is set, the day has an ending the machine acts
on itself: once the close has settled and the day's book is archived, the
runner stops every service that is still running, asks its own supervisor
whether anything is left, and - only when the answer is nothing - runs
``FRAMEWORK_SHUTDOWN_COMMAND`` to power the machine off. A service that will
not die leaves the machine up, with the reason on the operator's phone, rather
than pulling the plug on a process that might be mid-write.
"""

from __future__ import annotations

import argparse
import logging
import shlex
import signal
import subprocess  # noqa: S404 - running the machine's own shutdown command is the point
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any

import httpx

from common.holidays import HolidayList
from common.logging import setup_logging
from common.telegram import NullNotifier, escape, notifier_from_env
from common.window import IST, ist_now
from runner import schedule as day_schedule
from runner.config import RunnerSettings, load_settings
from runner.control import (
    SERVICE_LABELS,
    ControlState,
    create_control_app,
    serve,
    start_in_thread,
)
from runner.schedule import DayState
from runner.supervisor import (
    SERVICES,
    Supervisor,
    probe,
    stabilization_reason,
    wait_until_ready,
)

logger = logging.getLogger("runner")

#: How often the main loop looks at its children. Short enough that a crash is
#: noticed while the market is still moving, cheap enough to be free.
LOOP_SECONDS: float = 2.0

#: The day's services are resting because market data is gone. It is not a
#: market fact like the weekend, so it is tracked apart from the day's clock.
MODULE1_UNAVAILABLE: str = "Module 1 is not available"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="runner",
        description="Trading Framework v3 - start, supervise, and stop the four services",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the resolved configuration, send a Telegram test message, and exit",
    )
    parser.add_argument(
        "--any-time",
        action="store_true",
        help="ignore the trading window and keep everything running (development only)",
    )
    return parser.parse_args(argv)


def market_status(settings: RunnerSettings, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Module 1's status payload, for the stabilisation check."""

    owns = client is None
    holder = client or httpx.Client(timeout=5.0)
    try:
        response = holder.get(f"{settings.module1_url}/v1/market/status")
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns:
            holder.close()
    return payload.get("data") or {}


@dataclass(frozen=True, slots=True)
class MarketDay:
    """What the central process knows about the market on one day.

    ``trading`` is the one bit the day's schedule acts on. ``detail`` is why
    not, in words the Control tab can show, and ``holidays`` is the local
    list's dates, which the "next start" note uses so a holiday eve does not
    promise a morning that will not come.
    """

    trading: bool
    source: str
    detail: str | None = None
    holidays: frozenset[date] = frozenset()


@dataclass(frozen=True, slots=True)
class ArchiveSync:
    """Whether the day's book reached the long archive."""

    ok: bool
    message: str


def holiday_listings(settings: RunnerSettings) -> HolidayList:
    """The local holiday files this runner reads: the live list, then the seed."""

    return HolidayList((settings.holidays_file, settings.holidays_seed))


def market_day(
    settings: RunnerSettings,
    *,
    moment: datetime | None = None,
    holidays: HolidayList | None = None,
) -> MarketDay:
    """Whether the runner should start a trading day: weekday and holiday.

    The runner owns this check. It reads the local holiday list directly rather
    than asking Module 1, so a holiday is still recognised when Module 1 is
    starting, restarting or serving old data. Module 1 has no say in whether
    the trading services exist; it keeps fetching market data either way.

    A weekday with no holiday is a trading day even when the list does not cover
    the year. That is deliberate: the runner schedules a day it may find quiet
    rather than silently skipping a session because the calendar is stale.
    """

    now = moment or ist_now()
    today = now.astimezone(IST).date()
    listings = holidays or holiday_listings(settings)
    calendar = listings.current()
    known = frozenset(holiday.day for holiday in calendar.entries)

    entry = calendar.on(today)
    if entry is not None:
        return MarketDay(
            False,
            "local holiday list",
            f"NSE holiday: {entry.name} - no session today",
            known,
        )
    if today.weekday() >= 5:
        return MarketDay(
            False, "local weekday", "the weekend - no session today", known
        )
    return MarketDay(True, "local weekday", holidays=known)


def day_summary(settings: RunnerSettings, *, client: httpx.Client | None = None) -> str:
    """The day's book in one line, read from Module 3 before it is stopped."""

    owns = client is None
    holder = client or httpx.Client(timeout=5.0)
    try:
        response = holder.get(
            f"{settings.module3_url}/v1/orders/summary", params={"day": _today()}
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return f"(the day's summary could not be read: {type(exc).__name__})"
    finally:
        if owns:
            holder.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return "(the day's summary was empty)"
    return (
        f"{data.get('orders', 0)} orders, {data.get('closed', 0)} closed, "
        f"₹{float(data.get('realised') or 0):+.2f}, "
        f"{data.get('wins', 0)}W/{data.get('losses', 0)}L"
        + (
            f", avg {float(data['average_r']):+.2f}R"
            if data.get("average_r") is not None
            else ""
        )
    )


def sync_archive(
    settings: RunnerSettings, *, client: httpx.Client | None = None
) -> ArchiveSync:
    """Ask Module 4 to read Module 3's book one last time, before it stops.

    The archive's own timer does this every thirty seconds; this is the "now"
    that makes stopping the book safe, said in Module 4's own words rather than
    in the runner's hope.
    """

    owns = client is None
    holder = client or httpx.Client(timeout=10.0)
    try:
        response = holder.post(f"{settings.module4_url}/v1/sync")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return ArchiveSync(False, f"the archive sync failed ({type(exc).__name__})")
    finally:
        if owns:
            holder.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ArchiveSync(False, "the archive sync did not answer")
    if data.get("error"):
        return ArchiveSync(False, f"the archive sync failed: {data['error']}")
    remote = data.get("s3") or {}
    if remote.get("error"):
        return ArchiveSync(
            False,
            f"the S3 archive failed: {remote['error']}",
        )
    return ArchiveSync(
        True,
        f"archive synced ({data.get('seen', data.get('stored', data.get('orders', 0)))} orders seen"
        + (", S3 updated" if remote.get("uploaded") else "")
        + ")",
    )


def sync_agent_usage(
    settings: RunnerSettings, *, client: httpx.Client | None = None
) -> str:
    """Ask Module 4 to pull the day's token usage from Module 2 before it stops.

    The token counts live in Module 2's activity logs, and Module 2 is stopped
    just after the last decision. Module 4 owns the record and reads it over
    HTTP, so the last read has to happen while that door is still open. This is
    best effort: a missed total is logged, never a reason to keep the fleet up.
    """

    owns = client is None
    holder = client or httpx.Client(timeout=10.0)
    try:
        response = holder.post(f"{settings.module4_url}/v1/usage/sync")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return f"the token usage sync failed ({type(exc).__name__})"
    finally:
        if owns:
            holder.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return "the token usage sync did not answer"
    if data.get("error"):
        return f"the token usage sync failed: {data['error']}"
    return f"token usage synced ({data.get('days', 0)} days)"


def _today() -> str:
    return ist_now().date().isoformat()


def check(settings: RunnerSettings, notifier: Any) -> int:
    """Validate what can be validated without starting a service.

    Sends one real Telegram message, because "the token looks right" is not the
    question - the question is whether the operator's phone rings.
    """

    logger.info("configuration", extra=settings.describe())
    print("Trading Framework v3 - runner configuration")
    for key, value in settings.describe().items():
        print(f"  {key}: {value}")
    if isinstance(notifier, NullNotifier):
        print(f"\nTelegram: NOT configured ({notifier.reason})")
        return 1
    sent, error = notifier.send_now(
        "🧪 <b>Trading Framework</b> test message - the runner can reach Telegram."
    )
    if sent:
        print("\nTelegram: test message sent.")
        return 0
    print(f"\nTelegram: the test message failed ({error})")
    return 1


@dataclass(slots=True)
class RunnerState:
    """Everything the central process knows, shared by the loop and the site.

    The main loop writes; the control surface reads. The lock is not about the
    GIL: a snapshot that reads the day half-updated would report a moment that
    never existed.
    """

    settings: RunnerSettings
    supervisor: Supervisor
    notifier: Any
    started_at: datetime
    any_time: bool = False
    ready: bool = False
    wait_reason: str | None = "the status route has not answered yet"
    day: DayState = field(default_factory=DayState)
    #: The holiday dates the main loop last read from the local list, so the
    #: Control tab's "next start" skips a holiday without another file read.
    holidays: frozenset[date] = frozenset()
    attempts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    healthy: dict[str, bool] = field(default_factory=dict)
    last_health: float = 0.0
    closing_note: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)

    # -- the control surface's answers ---------------------------------------- #

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = now or ist_now()
        with self.lock:
            day = self.day.describe()
            attempts = dict(self.attempts)
            holidays = self.holidays
        return {
            "service": "framework_control",
            "version": "0.1.0",
            "checked_at": moment.isoformat(),
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": max(0.0, (moment - self.started_at).total_seconds()),
            "phase": self.phase(moment),
            "note": self.note(moment),
            "window": self.settings.window.describe(),
            "day": day,
            "next_day_start": day_schedule.next_day_start(
                moment,
                self.settings.window,
                self.settings.day_lead_seconds,
                skip_days=holidays,
            ).isoformat(),
            "market": self.market_block(moment),
            "restart_limit": self.settings.restart_limit,
            "attempts": attempts,
            "services": {name: self.service_report(name) for name in SERVICES},
        }

    def phase(self, moment: datetime) -> str:
        if not self.ready:
            return "starting"
        if self.any_time:
            return "development"
        return day_schedule.phase(
            moment,
            self.settings.window,
            self.day,
            lead_seconds=self.settings.day_lead_seconds,
            module2_stop_grace_seconds=self.settings.module2_stop_grace_seconds,
            close_grace_seconds=self.settings.close_grace_seconds,
        )

    def note(self, moment: datetime) -> str:
        phase = self.phase(moment)
        opens = self.settings.window.decision_open.strftime("%H:%M")
        closes = self.settings.window.close.strftime("%H:%M")
        if phase == "starting":
            return (
                "Module 1 is fetching history and computing indicators, and "
                "Module 4 (reporting) is running alongside it; the trading "
                "services start when Module 1 is ready."
            )
        if phase == "development":
            return "Development mode: the trading window is ignored and all four services run."
        if phase in {"closed_today", "pre_market", "day_over"}:
            # The *next* start, not today's: at 23:50 on a Saturday, "09:25
            # IST" without a day would read as twenty minutes away.
            upcoming = day_schedule.next_day_start(
                moment,
                self.settings.window,
                self.settings.day_lead_seconds,
                skip_days=self.holidays,
            )
            when = upcoming.strftime("%H:%M")
            if upcoming.date() != moment.astimezone(IST).date():
                when = f"{upcoming.strftime('%a %d %b')} {when}"
            return (
                "The market is not open. Module 1 keeps the last available data and "
                f"Module 4 keeps the reports; the trading services next start at {when} IST."
            )
        if phase == "after_decisions":
            return (
                "Decisions are over for the day; the order book is closing whatever "
                f"is still open, and nothing stays open past {closes} IST."
            )
        return (
            f"The framework is up: decisions {opens}-"
            f"{self.settings.window.decision_close.strftime('%H:%M')} IST, flat by {closes} IST."
        )

    def market_block(self, moment: datetime) -> dict[str, Any]:
        """The runner's own view of the exchange, from the local calendar."""

        local = moment.astimezone(IST)
        listings = holiday_listings(self.settings)
        calendar = listings.current()
        market = market_day(self.settings, moment=moment, holidays=listings)
        entry = calendar.on(local.date())
        exchange_hours = clock_time(9, 15) <= local.time() < clock_time(15, 30)
        opened = market.trading and exchange_hours

        next_day = local.date()
        if (
            local.time() >= clock_time(9, 15)
            or entry is not None
            or local.weekday() >= 5
        ):
            next_day = calendar.next_trading_day(next_day + timedelta(days=1))
        next_open = datetime.combine(next_day, clock_time(9, 15), tzinfo=IST)
        return {
            "open": opened,
            "source": "runner local list",
            "is_holiday": entry is not None,
            "holiday_name": None if entry is None else entry.name,
            "holidays_modelled": calendar.covers(local.date()),
            "holidays_fetched_at": (
                None if calendar.fetched_at is None else calendar.fetched_at.isoformat()
            ),
            "holidays_problem": calendar.problem,
            "exchange_time": local.strftime("%Y-%m-%d %H:%M:%S IST"),
            "session_open_ist": "09:15",
            "session_close_ist": "15:30",
            "next_open": next_open.isoformat(),
            "note": (
                "The runner reads the local NSE holiday list directly; Module 1 "
                "continues fetching market data regardless."
            ),
        }

    def service_report(self, name: str) -> dict[str, Any]:
        base = self.supervisor.describe(name)
        url = self.settings.urls()[name]
        scheduled = self.scheduled(name)
        health: bool | None = None
        health_detail: str | None = None
        if base["running"]:
            health, health_detail = probe(url, timeout=2.0)
        state, detail = self.service_state(
            name, base=base, scheduled=scheduled, health=health, health_detail=health_detail
        )
        return {
            "name": name,
            "label": SERVICE_LABELS.get(name, name),
            "url": url,
            "scheduled": scheduled,
            "state": state,
            "detail": detail,
            "health": health,
            "health_detail": health_detail,
            **base,
        }

    def scheduled(self, name: str) -> bool:
        # Market data and the archive are always wanted, from the first second:
        # Module 4 has nothing to wait for that Module 1 owns.
        if name in {"module1", "module4"}:
            return True
        if self.any_time:
            return True
        if not self.ready:
            return False
        with self.lock:
            day = self.day
            if not day.started or day.resting_reason:
                return False
            if name == "module2":
                return not day.module2_stopped
            if name == "module3":
                return not day.module3_stopped
        return False

    def service_state(
        self,
        name: str,
        *,
        base: dict[str, Any],
        scheduled: bool,
        health: bool | None,
        health_detail: str | None,
    ) -> tuple[str, str]:
        if self.attempts.get(name, 0) > self.settings.restart_limit:
            return (
                "failed",
                f"failed {self.attempts[name]} times and the restarts were stopped; "
                "use Restart once the cause is fixed",
            )
        if name == "module1" and not self.ready:
            wait = self.wait_reason or "stabilising"
            return "starting", f"the central process is waiting for it: {wait}"
        if base["running"]:
            if health is False:
                return "running", f"running, but its health check is failing ({health_detail})"
            return "running", f"answering on {self.settings.urls()[name]}"
        if name in {"module2", "module3"} and not scheduled:
            return "resting", self.resting_note(name)
        if not self.ready:
            if name == "module4":
                return "restarting", "starting alongside Module 1"
            return "waiting", "starts once Module 1 (market data) is ready"
        if scheduled:
            return "restarting", "starting now"
        return "stopped", "stopped"

    def resting_note(self, name: str) -> str:
        with self.lock:
            day = self.day
            reason = day.resting_reason
            started = day.started
            m2_stopped = day.module2_stopped
            m3_stopped = day.module3_stopped
        starts = day_schedule.day_start_at(
            ist_now(), self.settings.window, self.settings.day_lead_seconds
        ).strftime("%H:%M")
        if reason == MODULE1_UNAVAILABLE:
            return (
                "Module 1 is not available, so the trading services are stopped; "
                "restart Module 1 from this screen when the broker is reachable"
            )
        if reason:
            return reason
        if not day.trading_day:
            return "the runner's local holiday list says there is no session today"
        if not started:
            return f"the trading day starts about {starts} IST"
        if name == "module2" and m2_stopped:
            return (
                "the last decision moment has passed; the fleet starts again at "
                "the next session"
            )
        if name == "module3" and m3_stopped:
            return (
                "everything is flat and the day is archived; the book starts "
                "again at the next session"
            )
        return "the market is not open"

    # -- the one write -------------------------------------------------------- #

    def restart(self, name: str) -> dict[str, Any]:
        """Restart one service, from the Control tab.

        Restarting Module 1 is also the way back from its failure: the day's
        services are allowed to come back if the trading day is still live, so
        the operator does not have to restart three things in the right order.
        """

        report = self.supervisor.restart(name)
        with self.lock:
            self.attempts[name] = 0
            if name == "module1" and self.day.resting_reason == MODULE1_UNAVAILABLE:
                self.day = DayState(
                    day=self.day.day, trading_day=self.day.trading_day, started=False
                )
        logger.info("restarted %s on request (pid %s)", name, report.get("pid"))
        return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    setup_logging("runner")
    notifier = notifier_from_env("runner")
    logger.info("runner starting", extra=settings.describe())

    if args.check:
        code = check(settings, notifier)
        notifier.close()
        return code

    supervisor = Supervisor(root=settings.root, python=settings.python, log_dir=settings.log_dir)
    state = RunnerState(
        settings=settings,
        supervisor=supervisor,
        notifier=notifier,
        started_at=ist_now(),
        any_time=bool(args.any_time),
    )
    stop = threading.Event()

    def halt(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal signature
        logger.info("stopping on signal %s", signum)
        notifier.notify(
            f"🛑 <b>Runner</b> received signal {signum}; stopping the framework.",
            key="runner.signal",
            dedupe_seconds=0.0,
        )
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        held = getattr(signal, name, None)
        if held is not None:
            signal.signal(held, halt)

    # The control surface comes up before anything else, so the start-up the
    # operator watches on the Control tab is the real one.
    control = serve(
        create_control_app(
            ControlState(
                settings=settings,
                supervisor=supervisor,
                snapshot=state.snapshot,
                restart=state.restart,
            )
        ),
        settings,
    )
    control_thread = start_in_thread(control)
    logger.info("control surface on %s", settings.control_url)

    now = ist_now()
    if args.any_time:
        opening = "development mode: the trading window is ignored"
    elif settings.window.is_closed(now):
        opening = (
            f"started at {now.strftime('%H:%M')} IST, after the framework close "
            f"({settings.window.close.strftime('%H:%M')} IST): Module 1 and Module 4 "
            "run, and the trading services wait for the next session"
        )
    else:
        opening = (
            f"Module 1 and Module 4 first; Module 1 is given "
            f"{settings.stabilize_seconds:.0f}s to stabilise, and the trading "
            "services start about "
            f"{day_schedule.day_start_at(now, settings.window, settings.day_lead_seconds).strftime('%H:%M')} IST"
        )
    notifier.notify(
        f"🚀 <b>Runner</b> starting. {escape(opening)}.",
        key="runner.starting",
        dedupe_seconds=0.0,
    )

    # Market data and the archive are independent services: Module 4 reads a
    # local file and the order book, not Module 1, so it starts with Module 1
    # and is supervised from the same moment. Only the trading services wait
    # for Module 1's stabilisation, because only they depend on its data.
    supervisor.start("module1")
    supervisor.start("module4")
    ready, reason, waited = wait_until_ready(
        lambda: _status_or_raise(settings),
        clock=ist_now,
        minimum_seconds=settings.stabilize_seconds,
        timeout_seconds=settings.stabilize_timeout_seconds,
        indicator_fraction=settings.indicator_ready_fraction,
        should_stop=stop.is_set,
        report=lambda why: _note_wait(state, why),
    )
    if ready:
        _become_ready(state, waited)
    elif stop.is_set():
        logger.info("stopping before Module 1 became stable")
    else:
        logger.warning("Module 1 did not stabilise within %ss: %s", waited, reason)
        notifier.notify(
            "❌ <b>Module 1</b> did not stabilise "
            f"(waited {waited:.0f}s): {escape(reason or 'no reason recorded')}. "
            "The runner keeps watching; the rest of the framework waits.",
            key="runner.not_ready",
            dedupe_seconds=600.0,
        )

    client = httpx.Client(timeout=4.0)
    holidays = holiday_listings(settings)
    try:
        while not stop.is_set():
            now = ist_now()
            # The day's calendar is checked even while Module 1 warms up, so
            # the Control tab can already say "the market is closed today"
            # instead of promising a start that will not happen - and so a
            # holiday is never mistaken for a session.
            market = (
                MarketDay(True, "development", holidays=_holiday_dates(holidays))
                if state.any_time
                else market_day(settings, moment=now, holidays=holidays)
            )
            with state.lock:
                state.day.trading_day = market.trading
                state.holidays = market.holidays
            if not state.ready:
                _try_ready(state)
            elif not state.any_time:
                resting_before = state.day.resting_reason
                day, actions = day_schedule.advance(
                    state.day,
                    now,
                    settings.window,
                    lead_seconds=settings.day_lead_seconds,
                    trading_day=market.trading,
                    trading_day_reason=market.detail,
                    module2_stop_grace_seconds=settings.module2_stop_grace_seconds,
                    close_grace_seconds=settings.close_grace_seconds,
                )
                with state.lock:
                    state.day = day
                if day.resting_reason and day.resting_reason != resting_before:
                    _announce_resting(state, day.resting_reason)
                for action in actions:
                    _run_day_action(state, action, client)
                _shutdown_resting_day(state, day)
            _reap(state, stop)
            _check_health(state)
            stop.wait(LOOP_SECONDS)
    finally:
        summary = (
            day_summary(settings, client=client)
            if supervisor.is_running("module3")
            else state.closing_note or "(the order book was already stopped)"
        )
        stopped = supervisor.stop_all()
        logger.info("stopped: %s", ", ".join(stopped) or "nothing")
        notifier.notify(
            "🏁 <b>Framework stopped</b>. Day book: "
            + escape(summary)
            + (f". Services stopped: {escape(', '.join(stopped))}." if stopped else "."),
            key="runner.stopped",
            dedupe_seconds=0.0,
        )
        control.should_exit = True
        control_thread.join(timeout=5.0)
        notifier.flush(timeout=5.0)
        notifier.close()
    return 0


def _status_or_raise(settings: RunnerSettings) -> dict[str, Any]:
    """A status read that raises, so the wait loop can report the failure."""

    return market_status(settings)


def _become_ready(state: RunnerState, waited: float) -> None:
    """Module 1 is stable: say what comes next.

    Module 4 has been running since start-up. It is only started here if it is
    not running - a service stopped by hand while Module 1 warmed up should
    still be brought back - so the normal path is one notification and no new
    child process.
    """

    state.ready = True
    state.wait_reason = None
    if not state.supervisor.is_running("module4"):
        state.supervisor.start("module4")
        logger.info("Module 4 was not running; started it with Module 1 ready")
    took = f" after {waited:.0f}s" if waited > 0 else ""
    logger.info("Module 1 stable%s; the archive has been up since start-up", took)
    state.notifier.notify(
        f"✅ <b>Module 1</b> stable{took} - history fetched and "
        "indicators computed. <b>Module 4</b> (reporting) has been up since "
        "start-up; the trading services follow on the day's clock.",
        key="runner.stable",
        dedupe_seconds=0.0,
    )
    if state.any_time:
        state.supervisor.start("module3")
        state.supervisor.start("module2")


def _try_ready(state: RunnerState) -> None:
    """Re-check Module 1 while the rest waits, and start it the moment it is."""

    try:
        status = _status_or_raise(state.settings)
    except Exception as exc:  # noqa: BLE001 - a missing service is not ready
        _note_wait(state, f"{type(exc).__name__}: {exc}"[:120])
        return
    reason = stabilization_reason(
        status, indicator_fraction=state.settings.indicator_ready_fraction
    )
    if reason is None:
        _become_ready(state, 0.0)
        return
    _note_wait(state, reason)


def _note_wait(state: RunnerState, reason: str | None) -> None:
    if reason == state.wait_reason:
        return
    state.wait_reason = reason
    # The subject is the runner, not the module: "waiting for Module 1" alone
    # reads like Module 1 is waiting for itself.
    logger.info(
        "the central runner is waiting for Module 1 (market data): %s", reason
    )


def _holiday_dates(listings: HolidayList) -> frozenset[date]:
    return frozenset(holiday.day for holiday in listings.current().entries)


def _announce_resting(state: RunnerState, reason: str) -> None:
    """Say once why the trading services are resting today.

    The weekend needs no message - the operator knows the calendar - but a
    holiday closure is news worth sending, and it doubles as the day's "no
    trading today" note. The reason is written to the log either way, because
    the Control tab reads the day's state and the log keeps the record.
    """

    logger.info("the trading services rest today: %s", reason)
    if "NSE holiday" not in reason:
        return
    moment = ist_now()
    upcoming = day_schedule.next_day_start(
        moment,
        state.settings.window,
        state.settings.day_lead_seconds,
        skip_days=state.holidays,
    )
    when = upcoming.strftime("%H:%M")
    if upcoming.date() != moment.astimezone(IST).date():
        when = f"{upcoming.strftime('%a %d %b')} {when}"
    state.notifier.notify(
        "🗓 <b>No trading today</b>: "
        f"{escape(reason)}. Module 1 keeps market data and Module 4 keeps the "
        f"reports; the trading services next start at {when} IST.",
        key="runner.day_resting",
        dedupe_seconds=0.0,
    )


def _shutdown_resting_day(state: RunnerState, day: DayState) -> bool:
    """Power down a resting day once Module 1 has had its data pass.

    A holiday, a weekend and a process started after the decision window all
    reach the same state: Module 1 and Module 4 may have run, but there was no
    trading day. On a scheduled VM the machine must not stay up for the rest of
    the day. The guard is ``state.ready`` so the shutdown does not race Module
    1's own fetch; the failsafe timer remains for a Module 1 that never gets
    there.
    """

    if (
        not state.ready
        or not state.settings.self_shutdown
        or not day.resting_reason
        or day.shutdown_started
    ):
        return False
    day.shutdown_started = True
    with state.lock:
        state.day = day
    logger.info("the market is off and the day is resting; powering down")
    logger.info("resting-day self-shutdown: %s", power_down(state))
    return True


def _run_day_action(state: RunnerState, action: str, client: httpx.Client) -> None:
    """One transition of the trading day."""

    settings = state.settings
    if action == day_schedule.ACTION_START_DAY:
        # Module 3 first: the book is ready to receive a decision before an
        # agent can make one. Module 2 is started fresh, because its agents
        # finish for the day on their own clock, and yesterday's fleet must not
        # be mistaken for today's.
        state.supervisor.start("module3")
        if state.day.module2_stopped:
            # The schedule says the last decision moment has passed: the book
            # may still have positions to close, but a fleet started now could
            # only answer nothing - it would start, find the window over, and
            # stop, which is noise rather than work.
            logger.info(
                "the trading day started with the order book only: the last "
                "decision moment has passed, so no agent fleet is started"
            )
            opening = (
                "the order book is up for the close; the decision window is "
                "already over, so no agent fleet is started"
            )
        else:
            state.supervisor.restart("module2")
            logger.info("the trading day started: Modules 3 and 2 are up")
            opening = "the order book and the agent fleet are up for today's window"
        state.notifier.notify(
            f"🌅 <b>The trading day started</b>: {opening}.",
            key="runner.day_started",
            dedupe_seconds=0.0,
        )
        return
    if action == day_schedule.ACTION_STOP_MODULE2:
        # The agents' token counts live in Module 2's logs. Read them into
        # Module 4 first, while Module 2 is still answering, then stop it.
        usage = sync_agent_usage(settings, client=client)
        state.supervisor.stop("module2")
        logger.info(
            "the last decision moment has passed; Module 2 stopped (%s)", usage
        )
        state.notifier.notify(
            "🌙 <b>Module 2</b> stopped for the day; any open position is now "
            f"Module 3's to close by {settings.window.close.strftime('%H:%M')} IST.",
            key="runner.module2_stopped",
            dedupe_seconds=0.0,
        )
        return
    if action == day_schedule.ACTION_STOP_MODULE3:
        archived = sync_archive(settings, client=client)
        summary = day_summary(settings, client=client)
        state.closing_note = summary
        state.supervisor.stop("module3")
        logger.info("the day is over; Module 3 stopped (%s)", archived.message)
        if not archived.ok:
            state.notifier.notify(
                "🔴 <b>Market is off</b>, but the day's archive did not reach S3: "
                f"{escape(archived.message)}. The order book is stopped; the "
                "machine stays up so the archive can be fixed.",
                key="runner.archive_failed",
                dedupe_seconds=0.0,
            )
            return
        tail = (
            "The remaining services are closing; the machine powers itself "
            "off once they are gone."
            if settings.self_shutdown
            else "Module 1 and Module 4 keep running."
        )
        state.notifier.notify(
            "🔴 <b>Market is off</b> for the day. Day book: "
            + escape(summary)
            + f". {escape(archived.message)}. {tail}",
            key="runner.day_closed",
            dedupe_seconds=0.0,
        )
        if settings.self_shutdown:
            logger.info("self-shutdown: %s", power_down(state))
        return


def power_down(
    state: RunnerState,
    *,
    run_command: Callable[[str], tuple[bool, str | None]] | None = None,
) -> dict[str, Any]:
    """Close every remaining service, check, and only then power the machine off.

    The day is over: Module 2 is already stopped (or was never started), the
    book has been archived, and the only things still running are the services
    the runner would otherwise keep through the night. A scheduled machine
    should not. Each one is stopped, the supervisor is asked again whether
    anything is still running, and the power-off command is run only when the
    answer is nothing. A service that refuses to die leaves the machine up for
    the operator instead of pulling the plug on a process that might be
    mid-write, and the reason is said out loud rather than logged quietly.
    """

    settings = state.settings
    if not settings.self_shutdown:
        return {"skipped": "self-shutdown is disabled (FRAMEWORK_SELF_SHUTDOWN)"}
    supervisor = state.supervisor
    stopped: list[str] = []
    for name in ("module2", "module3", "module4", "module1"):
        if not supervisor.is_running(name):
            continue
        try:
            if supervisor.stop(name):
                stopped.append(name)
        except Exception as exc:  # noqa: BLE001 - a stuck child is not a crash here
            logger.warning("stopping %s failed: %s", name, type(exc).__name__)

    still_running = sorted(supervisor.running())
    if still_running:
        state.notifier.notify(
            "⚠️ <b>Framework</b> is not powering the machine off: "
            f"{escape(', '.join(still_running))} did not stop. Fix it on the "
            "Control tab, then power the machine off by hand.",
            key="runner.power_off_refused",
            dedupe_seconds=0.0,
        )
        return {"powered_off": False, "stopped": stopped, "still_running": still_running}

    execute = run_command or _run_shutdown_command
    state.notifier.notify(
        "🔴 <b>Market is off</b>. Every service is closed; powering the machine "
        f"off with <code>{escape(settings.shutdown_command)}</code>.",
        key="runner.power_off",
        dedupe_seconds=0.0,
    )
    # The message is the only sign of life after the machine goes: make sure
    # it is out of the queue before the command runs.
    state.notifier.flush(timeout=5.0)
    ok, error = execute(settings.shutdown_command)
    if not ok:
        state.notifier.notify(
            f"❌ <b>Framework</b> could not power the machine off: {escape(error)}. "
            "The machine is still running.",
            key="runner.power_off_failed",
            dedupe_seconds=0.0,
        )
        return {"powered_off": False, "stopped": stopped, "error": error}
    return {"powered_off": True, "stopped": stopped, "command": settings.shutdown_command}


def _run_shutdown_command(command: str) -> tuple[bool, str | None]:
    """Run the power-off command. ``(ran, why not)`` and never an exception.

    Split with ``shlex`` and never through a shell, so the setting is a
    command and its arguments rather than a script. The command is expected to
    return quickly - ``shutdown`` schedules the halt and exits - so a bounded
    timeout is enough to notice one that does not.
    """

    argv = shlex.split(command)
    if not argv:
        return False, "the shutdown command is empty"
    try:
        completed = subprocess.run(  # noqa: S603 - the operator's own command
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        return False, f"exit {completed.returncode}" + (f": {detail}" if detail else "")
    return True, None


def _reap(state: RunnerState, stop: threading.Event) -> None:
    """Restart a service that died, up to the limit, and say so either way."""

    supervisor = state.supervisor
    settings = state.settings
    for name, code in supervisor.reap():
        if name == "module2" and state.day.module2_stopped:
            continue
        if name == "module3" and state.day.module3_stopped:
            continue
        with state.lock:
            state.attempts[name] += 1
            attempts = state.attempts[name]
        if attempts > settings.restart_limit:
            state.notifier.notify(
                f"❌ <b>{escape(name)}</b> exited with code {code} and has now "
                f"failed {attempts} times; the runner has stopped trying to "
                "restart it. Check the Control tab, or "
                f"<code>{escape(settings.log_dir / (name + '.log'))}</code>.",
                key=f"runner.{name}.gave_up",
                dedupe_seconds=0.0,
            )
            if name == "module1":
                # The rest of the framework cannot work without market data, so
                # the trading services are stopped - but the central process,
                # the archive and the control surface stay, which is what makes
                # a restart from the operator's screen possible.
                state.notifier.notify(
                    "❌ <b>Module 1</b> is gone and nothing can trade without it; "
                    "the trading services are stopped. Use Restart on the Control "
                    "tab when the broker is reachable.",
                    key="runner.module1_gone",
                    dedupe_seconds=0.0,
                )
                supervisor.stop("module3")
                supervisor.stop("module2")
                with state.lock:
                    state.day.module2_stopped = True
                    state.day.module3_stopped = True
                    state.day.resting_reason = MODULE1_UNAVAILABLE
            continue
        state.notifier.notify(
            f"⚠️ <b>{escape(name)}</b> exited with code {code}; restarting it "
            f"({attempts} of {settings.restart_limit}).",
            key=f"runner.{name}.restart",
            dedupe_seconds=0.0,
        )
        stop.wait(settings.restart_delay_seconds)
        supervisor.start(name)


def _check_health(state: RunnerState) -> None:
    """Probe each running service's /health, reporting only transitions."""

    settings = state.settings
    if time.monotonic() - state.last_health < settings.health_seconds:
        return
    state.last_health = time.monotonic()
    for name, url in settings.urls().items():
        if not state.supervisor.is_running(name):
            continue
        ok, detail = probe(url)
        if ok == state.healthy.get(name, True):
            continue
        state.healthy[name] = ok
        if ok:
            state.notifier.notify(
                f"✅ <b>{escape(name)}</b> health is back.",
                key=f"runner.{name}.health_up",
                dedupe_seconds=0.0,
            )
        else:
            state.notifier.notify(
                f"⚠️ <b>{escape(name)}</b> health check is failing: {escape(detail)}",
                key=f"runner.{name}.health_down",
                dedupe_seconds=300.0,
            )


if __name__ == "__main__":
    raise SystemExit(main())

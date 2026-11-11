"""The central process's day: when Modules 3 and 2 exist, and when they stop.

Module 1 and Module 4 run for the life of the central process - market data
keeps the last available data and the archive keeps the reports, whether or not
the exchange is open. The trading day is different: the order book and the
agent engine only have work while the framework's window is open, so they are
started a lead before the first decision and stopped when their last piece of
work can no longer be due.

The rules live here as pure functions over the clock, so the one thing that
must never be guessed - when the framework stops trading - is decided in one
place that can be tested without starting a service.
"""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from common.window import IST, TradingWindow
from runner.supervisor import framework_stop_at, module2_stop_at

#: Start the day's services (Module 3 first, then Module 2).
ACTION_START_DAY: str = "start_day"
#: The agent fleet is done: no decision may start after the last boundary.
ACTION_STOP_MODULE2: str = "stop_module2"
#: The order book is done: everything is flat and the day can be archived.
ACTION_STOP_MODULE3: str = "stop_module3"


@dataclass(slots=True)
class DayState:
    """What the central process knows about the day it is living in."""

    #: The IST date this state belongs to. A new date resets everything.
    day: date | None = None
    #: Whether the exchange has a session today at all. The caller reads this
    #: from the runner's own local holiday list; the schedule never guesses it
    #: from the weekday on its own.
    trading_day: bool = True
    #: Whether today's trading services were started - or deliberately not,
    #: with ``resting_reason`` saying why.
    started: bool = False
    module2_stopped: bool = False
    module3_stopped: bool = False
    #: A resting day has asked the machine to power down already. It stops the
    #: main loop from trying again on every tick while the OS is shutting down.
    shutdown_started: bool = False
    #: Set when today's services are *not* running and should not be: the
    #: weekend, or a process started after the day was already over.
    resting_reason: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "day": None if self.day is None else self.day.isoformat(),
            "trading_day": self.trading_day,
            "started": self.started,
            "module2_stopped": self.module2_stopped,
            "module3_stopped": self.module3_stopped,
            "shutdown_started": self.shutdown_started,
            "resting_reason": self.resting_reason,
        }


def local(moment: datetime) -> datetime:
    return moment.astimezone(IST)


def day_start_at(moment: datetime, window: TradingWindow, lead_seconds: float) -> datetime:
    """The moment the day's services are started: a lead before the first decision."""

    return window.opens_at(moment) - timedelta(seconds=max(0.0, float(lead_seconds)))


def module3_stop_at(moment: datetime, window: TradingWindow, grace_seconds: float) -> datetime:
    """The moment the order book is stopped: after the close has settled."""

    return framework_stop_at(moment, window, grace_seconds)


def next_day_start(
    moment: datetime,
    window: TradingWindow,
    lead_seconds: float,
    *,
    skip_days: Set[date] = frozenset(),
) -> datetime:
    """The next moment the day's services will be started.

    ``skip_days`` are the exchange holidays the caller knows about: the
    arithmetic here can skip a weekend on its own, but "not a session" is a
    market fact and comes from the holiday list, never from this function.
    """

    current = local(moment)
    candidate = day_start_at(current, window, lead_seconds)
    if candidate <= current:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.date() in skip_days:
        candidate = candidate + timedelta(days=1)
    return candidate


def advance(
    state: DayState,
    moment: datetime,
    window: TradingWindow,
    *,
    lead_seconds: float,
    trading_day: bool = True,
    trading_day_reason: str | None = None,
    module2_stop_grace_seconds: float,
    close_grace_seconds: float,
) -> tuple[DayState, tuple[str, ...]]:
    """The day's plan for this moment: the new state, and what to do now.

    Called on every tick of the main loop, so it must be idempotent: an action
    is only ever returned once, because the state it changes is what suppresses
    it the next time. A moment that has already passed when the process starts
    is not replayed: the services that would have run then are reported as
    resting rather than started into a market that has moved on.

    ``trading_day_reason`` is the caller's explanation of *why* there is no
    session - a holiday name, or the weekend - and is shown instead of the
    generic sentence when there is one.
    """

    current = local(moment)
    today = current.date()
    if state.day != today:
        state = DayState(day=today, trading_day=bool(trading_day))
    else:
        state.trading_day = bool(trading_day)

    m2_stop = module2_stop_at(current, window, module2_stop_grace_seconds)
    m3_stop = module3_stop_at(current, window, close_grace_seconds)

    if not state.started:
        if not state.trading_day:
            state.started = True
            state.module2_stopped = True
            state.module3_stopped = True
            state.resting_reason = (
                trading_day_reason
                or "the runner's local holiday list says there is no session today"
            )
            return state, ()
        if current < day_start_at(current, window, lead_seconds):
            return state, ()
        if current >= window.decision_closes_at(current):
            # Started after the decision window closed: nothing is started
            # retroactively. There is no late fleet and no late order book;
            # an order book that was already running still gets its closing
            # time, but a process that starts now has nothing to close.
            state.started = True
            state.module2_stopped = True
            state.module3_stopped = True
            state.resting_reason = (
                "the decision window closed at "
                f"{window.decision_close.strftime('%H:%M')} IST"
            )
            return state, ()
        state.started = True
        return state, (ACTION_START_DAY,)

    if state.resting_reason:
        return state, ()

    actions: list[str] = []
    if not state.module2_stopped and current >= m2_stop:
        state.module2_stopped = True
        actions.append(ACTION_STOP_MODULE2)
    if not state.module3_stopped and current >= m3_stop:
        state.module3_stopped = True
        actions.append(ACTION_STOP_MODULE3)
    return state, tuple(actions)


def phase(
    moment: datetime,
    window: TradingWindow,
    state: DayState,
    *,
    lead_seconds: float,
    module2_stop_grace_seconds: float = 60.0,
    close_grace_seconds: float = 20.0,
) -> str:
    """A word for where the day is, for the control surface.

    One of ``closed_today``, ``pre_market``, ``open``, ``after_decisions``,
    ``day_over``.
    """

    current = local(moment)
    if not state.trading_day:
        return "closed_today"
    if not state.started:
        if current < day_start_at(current, window, lead_seconds):
            return "pre_market"
        return "day_over"
    if state.resting_reason:
        return "day_over"
    m2_stop = module2_stop_at(current, window, module2_stop_grace_seconds)
    m3_stop = module3_stop_at(current, window, close_grace_seconds)
    if current < m2_stop:
        return "open"
    if current < m3_stop:
        return "after_decisions"
    return "day_over"

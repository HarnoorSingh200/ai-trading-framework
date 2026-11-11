"""When an agent wakes up, on the exchange's clock.

The loop used to sleep until Module 1 announced that a candle had closed. That
is the wrong way round. An agent knows its own timeframe, and the exchange's
session has a fixed open, so it can work out for itself when its next candle
ends and be *ready at* the boundary rather than *told after* it. Waiting to be
notified also meant paying for two delays that had nothing to do with the work:
the notification's own latency, and a fixed settle period sitting behind it.

What this module deliberately does not own is the calendar. When the session
opens and closes is a market fact, and it comes from Module 1; this only does
the arithmetic on top of it. A holiday is not something to reason about here:
the runner reads the local holiday list directly and never starts the fleet on
one.

Bars are aligned to the session open rather than to the wall clock, because
that is how Module 1 builds them: the first 5-minute bar of an NSE day runs
09:15-09:20. An agent that woke on the hour would be deciding on a candle
nobody else has.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Final

from common.window import TradingWindow
from module2.timeutil import IST, parse_timestamp, to_ist, to_utc

#: The last minute of a session the broker publishes, counted from the open:
#: 09:15 to 15:14. Mirrors Module 1's ``AVAILABLE_MINUTES``, and for the same
#: reason - the cash segment's minute history ends there, so the last bar of a
#: day is the one holding 15:14.
PUBLISHED_MINUTES: Final[int] = 360

DEFAULT_OPEN: Final[time] = time(9, 15)
DEFAULT_CLOSE: Final[time] = time(15, 30)

#: How long each timeframe is, in minutes. Mirrors Module 1's list the way the
#: definition does, so an agent can work out its own schedule without asking.
MINUTES_BY_TIMEFRAME: Final[dict[str, int]] = {
    "1m": 1,
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "20m": 20,
    "30m": 30,
    "60m": 60,
    "1d": 1440,
}


def timeframe_minutes(value: str) -> int:
    """How long a timeframe is, in minutes."""

    return MINUTES_BY_TIMEFRAME.get(str(value).strip(), 15)


def parse_clock(value: object, fallback: time) -> time:
    """Read ``"09:15"``, or fall back to the exchange's usual hours."""

    text = str(value or "").strip()
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


def next_boundary(
    moment: datetime,
    *,
    minutes: int,
    session_open: time = DEFAULT_OPEN,
    session_close: time = DEFAULT_CLOSE,
) -> datetime:
    """The next moment a bar of this size ends, in UTC.

    Strictly after ``moment``: an agent that wakes exactly on a boundary and
    asks again must be sent to the *next* one, or it would spin on the boundary
    it has already decided.
    """

    size = max(1, int(minutes))
    local = to_ist(moment)
    opened = datetime.combine(local.date(), session_open, tzinfo=IST)
    closes = datetime.combine(local.date(), session_close, tzinfo=IST)

    if size >= 1440:
        # A daily bar ends with the session, not with the calendar.
        return to_utc(closes) if local < closes else to_utc(opened + timedelta(days=1))

    if local < opened:
        return to_utc(opened + timedelta(minutes=size))

    steps = int((local - opened).total_seconds() // (size * 60)) + 1
    return to_utc(opened + timedelta(minutes=steps * size))


def last_boundary(
    moment: datetime,
    *,
    minutes: int,
    session_open: time = DEFAULT_OPEN,
) -> datetime:
    """The last bar of the session that has data behind it, in UTC.

    Every timeframe this framework serves divides the 360 published minutes
    evenly, so this is 15:15 for all of them - the bucket holding 15:14.
    """

    size = max(1, int(minutes))
    local = to_ist(moment)
    opened = datetime.combine(local.date(), session_open, tzinfo=IST)
    if size >= 1440:
        return to_utc(opened)
    return to_utc(opened + timedelta(minutes=(PUBLISHED_MINUTES // size) * size))


def first_decision_boundary(
    moment: datetime,
    *,
    minutes: int,
    window: TradingWindow,
    session_open: time = DEFAULT_OPEN,
) -> datetime | None:
    """The first bar of the day that ends inside the decision window.

    Bars align to the exchange open, which is *not* the decision window's open:
    a 20-minute agent's first four bars end at 09:35, not at 09:30, so the first
    decision of the day has to be found by arithmetic rather than assumed.
    """

    size = max(1, int(minutes))
    if size >= 1440:
        # A daily bar closes with the session at 15:30, which is outside the
        # framework's day: a daily agent has no decision in this window.
        return None
    local = to_ist(moment)
    opened = datetime.combine(local.date(), session_open, tzinfo=IST)
    window_open = datetime.combine(local.date(), window.decision_open, tzinfo=IST)
    steps = max(0, math.ceil((window_open - opened).total_seconds() / (size * 60)))
    first = opened + timedelta(minutes=steps * size)
    close_at = datetime.combine(local.date(), window.decision_close, tzinfo=IST)
    if first > close_at:
        return None
    return to_utc(first)


def next_decision_boundary(
    moment: datetime,
    *,
    minutes: int,
    window: TradingWindow,
    session_open: time = DEFAULT_OPEN,
    session_close: time = DEFAULT_CLOSE,
    settle_seconds: float = 0.0,
) -> datetime | None:
    """When an agent should next look, or ``None`` when the day is done.

    The moment returned is the bar's **close plus the settle buffer**: Module 1
    only closes a minute when the first tick of the next one arrives, so the
    candle (and the indicators computed from it) need a moment to exist before
    an agent reads them. Waking exactly on the boundary and reading instantly
    was the old guess; five seconds is the measured one.

    ``None`` means there is no further bar to decide today - the next one would
    close after the decision window - and the caller should wait for the next
    session rather than spin on the boundary it has already passed.
    """

    size = max(1, int(minutes))
    first = first_decision_boundary(
        moment, minutes=size, window=window, session_open=session_open
    )
    if first is None:
        return None
    local = to_ist(moment)
    candidate = next_boundary(
        moment, minutes=size, session_open=session_open, session_close=session_close
    )
    if candidate < first:
        candidate = first
    close_at = datetime.combine(local.date(), window.decision_close, tzinfo=IST)
    if to_ist(candidate) > close_at:
        return None
    return to_utc(to_ist(candidate) + timedelta(seconds=max(0.0, float(settle_seconds))))


def wait_seconds(moment: datetime, target: datetime) -> float:
    """How long to sleep for, never negative."""

    return max(0.0, (to_utc(target) - to_utc(moment)).total_seconds())


def reopen_after(moment: datetime, session: dict[str, object]) -> datetime | None:
    """When the next session opens, as Module 1 reports it."""

    return parse_timestamp(session.get("next_open"))


__all__ = [
    "MINUTES_BY_TIMEFRAME",
    "PUBLISHED_MINUTES",
    "first_decision_boundary",
    "last_boundary",
    "next_boundary",
    "next_decision_boundary",
    "parse_clock",
    "reopen_after",
    "timeframe_minutes",
    "wait_seconds",
]

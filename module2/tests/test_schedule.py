"""When an agent wakes up: the arithmetic, and the mistakes it prevents."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from module2.schedule import (
    last_boundary,
    next_boundary,
    parse_clock,
    timeframe_minutes,
    wait_seconds,
)
from module2.timeutil import IST, to_ist

pytestmark = pytest.mark.unit


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, second, tzinfo=IST)


def test_the_next_boundary_is_aligned_to_the_session_open_not_the_hour() -> None:
    """Bars run 09:15-09:35, so a 20-minute agent wakes at :35, not at :00."""

    boundary = next_boundary(at(9, 40), minutes=20)

    assert to_ist(boundary).strftime("%H:%M") == "09:55"


def test_a_five_minute_agent_wakes_on_the_five_minute_mark() -> None:
    assert to_ist(next_boundary(at(10, 17), minutes=5)).strftime("%H:%M") == "10:20"


def test_a_boundary_that_has_just_passed_sends_you_to_the_next_one() -> None:
    """An agent that wakes exactly on the boundary and asks again must move on,
    or it would sit deciding the same candle for ever."""

    assert to_ist(next_boundary(at(10, 15), minutes=5)).strftime("%H:%M") == "10:20"


def test_before_the_open_the_first_wake_is_one_bar_in() -> None:
    assert to_ist(next_boundary(at(8, 0), minutes=5)).strftime("%H:%M") == "09:20"


def test_the_last_boundary_of_a_session_holds_the_last_published_minute() -> None:
    """Every timeframe divides the 360 published minutes, so the last bar of a
    day ends at 15:15 whatever the size."""

    for minutes in (1, 5, 10, 15, 20, 30, 60):
        last = last_boundary(at(12, 0), minutes=minutes)
        assert to_ist(last).strftime("%H:%M") == "15:15", minutes


def test_a_daily_bar_ends_with_the_session() -> None:
    assert to_ist(next_boundary(at(10, 0), minutes=1440)).strftime("%H:%M") == "15:30"


def test_after_the_close_a_daily_agent_waits_for_the_next_session() -> None:
    assert to_ist(next_boundary(at(16, 0), minutes=1440)).strftime("%H:%M") == "09:15"


def test_the_session_hours_come_from_module_1() -> None:
    """A different open moves every boundary with it."""

    boundary = next_boundary(at(9, 16), minutes=15, session_open=time(9, 0))

    assert to_ist(boundary).strftime("%H:%M") == "09:30"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("09:15", time(9, 15)), ("15:30", time(15, 30)), ("", time(9, 15)), (None, time(9, 15))],
)
def test_a_clock_is_read_or_falls_back_to_the_exchange_hours(
    text: object, expected: time
) -> None:
    fallback = time(9, 15)
    assert parse_clock(text, fallback) == (expected if text else fallback)


def test_every_timeframe_the_framework_serves_has_a_size() -> None:
    assert [timeframe_minutes(value) for value in ("1m", "5m", "15m", "60m", "1d")] == [
        1,
        5,
        15,
        60,
        1440,
    ]


def test_waiting_for_a_moment_that_has_passed_is_no_wait_at_all() -> None:
    assert wait_seconds(at(10, 0), at(9, 0)) == 0.0
    assert wait_seconds(at(10, 0), at(10, 0) + timedelta(seconds=5)) == 5.0

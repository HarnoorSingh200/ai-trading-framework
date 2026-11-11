"""The session clock: hours, weekends and the exchange's holidays."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from common.holidays import Holiday, HolidayCalendar
from module1.session_clock import describe, install_calendar, is_open, next_open, reset_calendar
from module1.timeutil import IST

pytestmark = pytest.mark.unit

#: Holi 2026: a Tuesday, so the neighbouring Monday and Wednesday are sessions.
HOLI: date = date(2026, 3, 3)


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in March 2026 IST: the month the holiday fixture lives in."""

    return datetime(2026, 3, day, hour, minute, tzinfo=IST)


def holi_calendar() -> HolidayCalendar:
    return HolidayCalendar(
        [Holiday(HOLI, "Holi")],
        source="test",
        fetched_at=datetime(2026, 3, 1, 9, 0, tzinfo=IST),
    )


def test_a_regular_session_runs_between_the_bells() -> None:
    calendar = HolidayCalendar()

    assert is_open(moment(2, 9, 14), calendar=calendar) is False
    assert is_open(moment(2, 9, 15), calendar=calendar) is True
    assert is_open(moment(2, 15, 29), calendar=calendar) is True
    assert is_open(moment(2, 15, 30), calendar=calendar) is False


def test_a_weekend_is_closed() -> None:
    calendar = HolidayCalendar()

    assert is_open(moment(7, 12, 0), calendar=calendar) is False
    assert is_open(moment(8, 12, 0), calendar=calendar) is False


def test_a_holiday_is_closed_all_day_and_says_so() -> None:
    calendar = holi_calendar()

    assert is_open(moment(3, 10, 0), calendar=calendar) is False

    described = describe(moment(3, 10, 0), calendar=calendar)

    assert described["is_open"] is False
    assert described["is_holiday"] is True
    assert described["holiday_name"] == "Holi"
    assert described["holidays_modelled"] is True
    assert "Holi" in described["note"]


def test_the_next_open_skips_a_holiday_and_the_weekend() -> None:
    calendar = holi_calendar()

    # Monday after the close: Tuesday is Holi, so the next session is Wednesday.
    assert next_open(moment(2, 16, 0), calendar=calendar) == moment(4, 9, 15)
    # On the holiday itself, before its (non-existent) bell.
    assert next_open(moment(3, 8, 0), calendar=calendar) == moment(4, 9, 15)
    # The Friday after: the weekend follows.
    assert next_open(moment(6, 16, 0), calendar=calendar) == moment(9, 9, 15)


def test_the_next_open_is_today_before_the_bell() -> None:
    assert next_open(moment(2, 8, 0), calendar=HolidayCalendar()) == moment(2, 9, 15)


def test_a_year_the_list_does_not_cover_is_reported_as_unknown() -> None:
    # The list knows 2026; a 2027 session is scheduled, but the clock says that
    # its "no holiday" rests on no data rather than on a fetched fact.
    described = describe(
        datetime(2027, 1, 4, 10, 0, tzinfo=IST), calendar=holi_calendar()
    )

    assert described["holidays_modelled"] is False
    assert "does not cover 2027" in described["note"]
    assert described["is_open"] is True


def test_a_problem_with_the_list_is_published() -> None:
    calendar = HolidayCalendar(problem="not valid JSON")

    described = describe(moment(2, 10, 0), calendar=calendar)

    assert described["holidays_problem"] == "not valid JSON"
    assert described["holidays_modelled"] is False


def test_install_calendar_points_the_clock_at_a_list() -> None:
    reset_calendar()
    install_calendar(holi_calendar())

    described = describe(moment(3, 10, 0))

    assert described["is_holiday"] is True

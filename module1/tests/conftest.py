"""Module 1's tests run against a known-empty holiday list.

The session clock's default is the machine's real file, and a test that happens
to run on a holiday would behave differently from the same test a day later.
Tests that want holidays install their own list; every other test gets an empty
one, so "no holiday" means "none in the test", not "none in the world".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from common.holidays import HolidayCalendar
from module1 import session_clock


@pytest.fixture(autouse=True)
def isolated_holiday_calendar() -> Iterator[None]:
    session_clock.install_calendar(HolidayCalendar())
    yield
    session_clock.reset_calendar()

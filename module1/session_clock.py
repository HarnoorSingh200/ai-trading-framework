"""Where we are in the trading day.

The exchange clock, the weekend and the exchange's holiday list are modelled
here. The UI and Module 2/3 label what they are showing with it, and Module 1
uses it when it needs a previous trading session. The runner's decision to
start Modules 2 and 3 is separate: it reads the local holiday file itself and
does not ask Module 1, so a holiday is still a resting day while Module 1 keeps
fetching data.

The list itself lives in :mod:`common.holidays`: a local JSON file, seeded from
the checked-in copy and refreshed from NSE's holiday master by this module's
service. The clock never fetches; the service installs the loaded list with
:func:`install_calendar`, and a clock with no list says so rather than
pretending every weekday is a session.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from common.holidays import HolidayCalendar, HolidayList, resolve_paths
from module1.config import PROJECT_ROOT
from module1.timeutil import IST, format_ist, to_ist, utc_now

#: Regular NSE equity session, in IST.
MARKET_OPEN: Final[time] = time(9, 15)
MARKET_CLOSE: Final[time] = time(15, 30)

PHASE_OPEN: Final[str] = "open"
PHASE_CLOSED: Final[str] = "closed"

#: The loaded list, installed by the service (and by tests). The lazy default
#: exists for callers outside the service - a test or a script - and reads the
#: same files the service would.
_calendar: HolidayCalendar | None = None
_calendar_list: HolidayList | None = None
_calendar_lock = threading.Lock()


def install_calendar(calendar: HolidayCalendar | None) -> None:
    """Point the clock at a loaded holiday list.

    Called by the service at start-up and after every successful refresh; also
    the seam tests use so a test never reads the machine's real list.
    """

    global _calendar
    with _calendar_lock:
        _calendar = calendar


def reset_calendar() -> None:
    """Forget the installed list, so the next call re-reads the local files."""

    global _calendar, _calendar_list
    with _calendar_lock:
        _calendar = None
        _calendar_list = None


def default_calendar() -> HolidayCalendar:
    """The list in force: the installed one, or the local files."""

    global _calendar, _calendar_list
    with _calendar_lock:
        if _calendar is None:
            if _calendar_list is None:
                _calendar_list = HolidayList(resolve_paths(PROJECT_ROOT))
            _calendar = _calendar_list.current()
        return _calendar


def is_open(
    moment: datetime | None = None, *, calendar: HolidayCalendar | None = None
) -> bool:
    """Whether the regular session is running at ``moment``.

    Weekends and exchange holidays are excluded.
    """

    local = to_ist(moment or utc_now())
    resolved = calendar or default_calendar()
    if local.weekday() >= 5 or resolved.is_holiday(local.date()):
        return False
    return MARKET_OPEN <= local.timetz().replace(tzinfo=None) < MARKET_CLOSE


def next_open(
    moment: datetime | None = None, *, calendar: HolidayCalendar | None = None
) -> datetime:
    """The next moment a regular session starts, in UTC.

    Skips weekends *and* holidays, because the caller uses this to decide how
    long to rest: promising a session on a closed day would be a wake-up into
    an empty market.
    """

    resolved = calendar or default_calendar()
    local = to_ist(moment or utc_now())
    day = local.date()
    if local.timetz().replace(tzinfo=None) >= MARKET_OPEN:
        day = day + timedelta(days=1)
    return datetime.combine(resolved.next_trading_day(day), MARKET_OPEN, tzinfo=IST)


def describe(
    moment: datetime | None = None, *, calendar: HolidayCalendar | None = None
) -> dict[str, Any]:
    """A small, safe summary for the UI's header and status panels."""

    now = moment or utc_now()
    local = to_ist(now)
    resolved = calendar or default_calendar()
    opened = is_open(now, calendar=resolved)
    closes_at = datetime.combine(local.date(), MARKET_CLOSE, tzinfo=IST)
    minutes_to_close = (
        int((closes_at - local).total_seconds() // 60) if opened else None
    )
    holidays = resolved.describe(local.date())
    return {
        "phase": PHASE_OPEN if opened else PHASE_CLOSED,
        "is_open": opened,
        "exchange": "NSE",
        "exchange_time": local.isoformat(),
        "exchange_time_label": format_ist(now),
        "session_open_ist": MARKET_OPEN.strftime("%H:%M"),
        "session_close_ist": MARKET_CLOSE.strftime("%H:%M"),
        "minutes_to_close": minutes_to_close,
        "next_open": next_open(now, calendar=resolved).isoformat(),
        **holidays,
        "note": _note(holidays, local.date()),
    }


def _note(holidays: dict[str, Any], day: date) -> str:
    if holidays["is_holiday"]:
        return (
            f"NSE holiday: {holidays['holiday_name']} - "
            "the exchange has no session today."
        )
    if not holidays["holidays_modelled"]:
        return (
            f"The local holiday list does not cover {day.year}; sessions follow "
            "the weekday calendar until it is refreshed from NSE."
        )
    return "Regular NSE session hours; the local holiday list has no holiday today."

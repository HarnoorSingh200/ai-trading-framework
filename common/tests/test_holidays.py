"""The local holiday list: reading it, fetching it, and staying honest."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from common.holidays import (
    Holiday,
    HolidayCalendar,
    HolidayFetchError,
    HolidayList,
    fetch_nse_holidays,
    load,
    refresh,
    resolve_paths,
    write,
)
from common.window import IST

pytestmark = pytest.mark.unit


def payload(*holidays: tuple[str, str], years: int = 2026) -> dict[str, object]:
    return {
        "source": "nse-holiday-master",
        "source_url": "https://www.nseindia.com/api/holiday-master?type=trading",
        "segment": "CM",
        "fetched_at": f"{years}-10-17T09:00:00+05:30",
        "holidays": [{"date": day, "name": name} for day, name in holidays],
    }


def written(tmp_path: Path, *holidays: tuple[str, str]) -> Path:
    path = tmp_path / "nse.json"
    path.write_text(json.dumps(payload(*holidays)), encoding="utf-8")
    return path


def test_a_list_is_read_from_json(tmp_path: Path) -> None:
    path = written(
        tmp_path,
        ("2026-01-26", "Republic Day"),
        ("2026-03-03", "Holi"),
    )

    calendar = load(path)

    assert calendar.problem is None
    assert calendar.is_holiday(date(2026, 3, 3)) is True
    assert calendar.is_holiday(date(2026, 3, 4)) is False
    assert calendar.on(date(2026, 1, 26)).name == "Republic Day"
    assert calendar.years() == (2026,)
    assert calendar.covers(date(2026, 12, 25)) is True
    assert calendar.covers(date(2027, 1, 1)) is False
    assert calendar.fetched_at == datetime(2026, 10, 17, 9, 0, tzinfo=IST)
    assert calendar.source == "nse-holiday-master"


def test_a_missing_file_is_an_empty_calendar_not_an_error(tmp_path: Path) -> None:
    calendar = load(tmp_path / "nothing.json")

    assert calendar.entries == ()
    assert calendar.problem is None
    assert calendar.is_holiday(date(2026, 3, 3)) is False
    # "No holiday" is not a claim this calendar may make about 2026: it has
    # never heard of the year.
    assert calendar.covers(date(2026, 3, 3)) is False


def test_a_broken_file_reports_its_problem_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "nse.json"
    path.write_text("this is not json", encoding="utf-8")

    calendar = load(path)

    assert calendar.entries == ()
    assert calendar.problem == "not valid JSON"


def test_a_half_read_row_is_reported_and_the_rest_kept(tmp_path: Path) -> None:
    body = payload(("2026-01-26", "Republic Day"))
    body["holidays"].append({"date": "not-a-date", "name": "?"})  # type: ignore[union-attr]
    path = tmp_path / "nse.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    calendar = load(path)

    assert len(calendar.entries) == 1
    assert calendar.problem is not None and "1 rows" in calendar.problem


def test_a_bare_list_of_dates_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "nse.json"
    path.write_text(json.dumps(["2026-01-26", "2026-03-03"]), encoding="utf-8")

    calendar = load(path)

    assert calendar.is_holiday(date(2026, 1, 26)) is True
    assert calendar.on(date(2026, 3, 3)).name == "Holiday"


def test_next_trading_day_skips_weekends_and_holidays() -> None:
    # 2026-03-02 is a Monday; the 3rd is Holi, and the 7th-8th are the weekend.
    calendar = HolidayCalendar([Holiday(date(2026, 3, 3), "Holi")])

    assert calendar.next_trading_day(date(2026, 3, 2)) == date(2026, 3, 2)
    assert calendar.next_trading_day(date(2026, 3, 3)) == date(2026, 3, 4)
    assert calendar.next_trading_day(date(2026, 3, 6)) == date(2026, 3, 6)
    assert calendar.next_trading_day(date(2026, 3, 7)) == date(2026, 3, 9)


def test_the_second_file_is_used_when_the_first_is_missing(tmp_path: Path) -> None:
    seed = written(tmp_path, ("2026-01-26", "Republic Day"))
    listings = HolidayList([tmp_path / "live.json", seed])

    calendar = listings.current()

    assert calendar.is_holiday(date(2026, 1, 26)) is True
    assert calendar.path == seed


def test_the_live_file_wins_once_it_exists_and_changes_are_re_read(
    tmp_path: Path,
) -> None:
    seed = written(tmp_path, ("2026-01-26", "Republic Day"))
    live = tmp_path / "live.json"
    listings = HolidayList([live, seed])
    assert listings.current().path == seed

    write(live, holidays=[Holiday(date(2026, 8, 15), "Independence Day")])

    calendar = listings.current()
    assert calendar.path == live
    assert calendar.is_holiday(date(2026, 8, 15)) is True
    assert calendar.is_holiday(date(2026, 1, 26)) is False


def test_a_broken_live_file_falls_back_to_the_seed(tmp_path: Path) -> None:
    seed = written(tmp_path, ("2026-01-26", "Republic Day"))
    live = tmp_path / "live.json"
    live.write_text("{ half a file", encoding="utf-8")

    calendar = HolidayList([live, seed]).current()

    assert calendar.path == seed
    assert calendar.problem is None


def nse_client(
    *, rows: list[dict[str, str]] | None = None, api_status: int = 200
) -> httpx.Client:
    """A stand-in for NSE: a page that warms the session, then the API."""

    body = {
        "CM": rows
        or [
            {
                "tradingDate": "26-Jan-2026",
                "weekDay": "Monday",
                "description": "Republic Day",
            },
            {
                "tradingDate": "03-Mar-2026",
                "weekDay": "Tuesday",
                "description": "Holi",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/" in request.url.path:
            if api_status != 200:
                return httpx.Response(api_status, text="busy")
            return httpx.Response(200, json=body)
        return httpx.Response(200, text="<html>holidays</html>")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_nse_answer_is_parsed_into_sorted_holidays() -> None:
    with nse_client() as client:
        fetched = fetch_nse_holidays(client=client)

    assert fetched == (
        Holiday(date(2026, 1, 26), "Republic Day"),
        Holiday(date(2026, 3, 3), "Holi"),
    )


def test_a_failed_fetch_is_an_error_the_caller_can_swallow() -> None:
    with nse_client(api_status=503) as client, pytest.raises(HolidayFetchError):
        fetch_nse_holidays(client=client)


def test_a_fetch_replaces_the_years_it_returns_and_keeps_the_others(
    tmp_path: Path,
) -> None:
    path = written(tmp_path, ("2026-01-26", "Republic Day"))

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "CM": [
                        {
                            "tradingDate": "26-Jan-2027",
                            "weekDay": "Tuesday",
                            "description": "Republic Day",
                        }
                    ]
                },
            )
        return httpx.Response(200, text="<html>holidays</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refreshed = refresh(path, client=client)

    assert refreshed.is_holiday(date(2026, 1, 26)) is True
    assert refreshed.is_holiday(date(2027, 1, 26)) is True
    reread = load(path)
    assert reread.years() == (2026, 2027)
    assert reread.fetched_at is not None


def test_a_failed_refresh_leaves_the_local_list_alone(tmp_path: Path) -> None:
    path = written(tmp_path, ("2026-01-26", "Republic Day"))
    before = path.read_text(encoding="utf-8")

    with nse_client(api_status=503) as client, pytest.raises(HolidayFetchError):
        refresh(path, client=client)

    assert path.read_text(encoding="utf-8") == before
    assert load(path).is_holiday(date(2026, 1, 26)) is True


def test_needs_refresh_when_missing_old_or_uncovered() -> None:
    now = datetime(2026, 10, 17, 9, 0, tzinfo=IST)
    fresh = HolidayCalendar(
        [Holiday(date(2026, 1, 26), "Republic Day")],
        fetched_at=now - timedelta(hours=2),
    )
    old = HolidayCalendar(
        [Holiday(date(2026, 1, 26), "Republic Day")],
        fetched_at=now - timedelta(days=3),
    )
    uncovered = HolidayCalendar(
        [Holiday(date(2025, 1, 26), "Republic Day")],
        fetched_at=now - timedelta(hours=2),
    )

    assert fresh.needs_refresh(now=now, refresh_seconds=86400) is False
    assert old.needs_refresh(now=now, refresh_seconds=86400) is True
    assert uncovered.needs_refresh(now=now, refresh_seconds=86400) is True
    assert HolidayCalendar().needs_refresh(now=now, refresh_seconds=86400) is True


def test_the_describe_block_says_where_the_list_came_from(tmp_path: Path) -> None:
    path = written(tmp_path, ("2026-01-26", "Republic Day"))
    calendar = load(path)

    described = calendar.describe(date(2026, 1, 26))

    assert described["holidays_modelled"] is True
    assert described["is_holiday"] is True
    assert described["holiday_name"] == "Republic Day"
    assert described["holidays_file"] == str(path)
    assert described["holidays_fetched_at"] == "2026-10-17T09:00:00+05:30"
    assert described["holidays_problem"] is None


def test_the_paths_follow_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRAMEWORK_HOLIDAYS_FILE", raising=False)

    live, seed = resolve_paths(tmp_path)

    assert live == tmp_path / "data" / "holidays" / "nse.json"
    assert seed == tmp_path / "module1" / "data" / "nse_holidays.json"

    monkeypatch.setenv("FRAMEWORK_HOLIDAYS_FILE", "elsewhere/list.json")

    live, _ = resolve_paths(tmp_path)
    assert live == tmp_path / "elsewhere" / "list.json"

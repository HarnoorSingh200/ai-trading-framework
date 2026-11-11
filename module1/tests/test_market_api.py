"""The four reads Module 2's tools are built on, through the real HTTP surface.

These are thin routes over services that are tested in their own right, so what
is checked here is the part that only exists at the edge: the shape of what goes
out, and that a refusal names the field or symbol that was wrong rather than
just handing back a bare 400.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from module1.analytics import IndicatorService
from module1.broker.kite import Candle
from module1.builder import CandleBook
from module1.candles import CandleService
from module1.service import create_app
from module1.timeutil import IST

pytestmark = pytest.mark.unit

SESSION_DAY = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
MID_BUCKET = SESSION_DAY + timedelta(minutes=368)  # 15:23
CLOSED_OPEN = SESSION_DAY + timedelta(minutes=345)  # 15:00


class FakeClient:
    def historical(self, *args: Any, **kwargs: Any) -> list[Candle]:
        return []

    def day_candles(
        self, token: str, *, sessions: int, now: Any = None, interactive: bool = False
    ) -> list[Candle]:
        return [
            Candle(
                open_time=SESSION_DAY - timedelta(days=3),
                open=Decimal("100"),
                high=Decimal("105.50"),
                low=Decimal("98.25"),
                close=Decimal("103.75"),
                volume=Decimal("1000"),
            )
        ]


def minutes(count: int = 368) -> list[Candle]:
    rows: list[Candle] = []
    for index in range(count):
        close = Decimal(f"{100 + index * 0.01:.2f}")
        rows.append(
            Candle(
                open_time=SESSION_DAY + timedelta(minutes=index),
                open=close,
                high=close + Decimal("0.40"),
                low=close - Decimal("0.35"),
                close=close,
                volume=Decimal(str(1000 + index)),
            )
        )
    return rows


def client_for(
    *symbols: str, now: datetime | None = None, count: int = 368
) -> tuple[TestClient, Any]:
    moment = now or MID_BUCKET
    book = CandleBook()
    service = CandleService(
        FakeClient(),
        resolve_token=lambda symbol: "1" if symbol in symbols else None,
        book=book,
        symbol_meta=lambda symbol: {
            "name": f"{symbol} Ltd.",
            "industry": "IT",
            "instrument_token": "1",
            "last_price": 103.67,
            "day_change_percent": 0.65,
            "day_low": 99.5,
            "day_high": 104.2,
            "day_volume": 12345.0,
            "price_source": "live",
            "price_age_seconds": 3.0,
        },
        clock=lambda: moment,
    )
    indicators = IndicatorService(candles=service, book=book, clock=lambda: moment)
    service.set_indicator_provider(
        indicators.latest, indicators.describe, indicators.closed_frame
    )
    for symbol in symbols:
        book.seed(symbol, minutes(count))
    indicators.refresh_stale(limit=50)

    runtime = SimpleNamespace(candles=service, indicators=indicators, book=book)
    return TestClient(create_app(runtime)), service  # type: ignore[arg-type]


def test_the_field_catalogue_is_served_over_http() -> None:
    client, _ = client_for("AAA")

    payload = client.get("/v1/market/fields?interval=15m").json()["data"]

    names = {field["name"] for field in payload["fields"]}
    assert {"rsi_14", "close", "volume_ratio", "industry"} <= names
    assert payload["interval"] == "15m"
    assert payload["limit"]["default"] == 5
    assert payload["groups"]


def test_a_screen_returns_the_shortlist_and_the_candle_it_describes() -> None:
    client, _ = client_for("AAA", "BBB")

    response = client.post(
        "/v1/market/screen",
        json={
            "interval": "15m",
            "where": [{"field": "rsi_14", "op": ">", "value": 0}],
            "rank_by": "volume_ratio",
            "direction": "desc",
            "limit": 2,
            "fields": ["rsi_14", "volume_ratio"],
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["interval"] == "15m"
    assert data["bar_open_time"] == CLOSED_OPEN.isoformat()
    assert data["criteria"]["rank_by"] == "volume_ratio"
    assert [row["symbol"] for row in data["rows"]] == ["AAA", "BBB"]
    assert set(data["rows"][0]) == {
        "symbol",
        "name",
        "industry",
        "rsi_14",
        "volume_ratio",
    }


def test_a_screen_that_names_an_unknown_field_is_refused_with_the_field() -> None:
    client, _ = client_for("AAA")

    response = client.post(
        "/v1/market/screen",
        json={"interval": "15m", "where": [{"field": "rsi", "op": "<", "value": 30}]},
    )

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "SCREEN_INVALID"
    assert error["details"]["field"] == "rsi"
    assert "rsi" in error["message"]


def test_a_screen_on_an_unknown_timeframe_is_refused_with_the_list() -> None:
    client, _ = client_for("AAA")

    response = client.post("/v1/market/screen", json={"interval": "9m"})

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "TIMEFRAME_UNKNOWN"
    assert "15m" in error["details"]["supported"]


def test_the_symbol_bundle_answers_in_one_call() -> None:
    client, _ = client_for("AAA")

    data = client.get("/v1/market/symbol/AAA?interval=15m").json()["data"]

    assert data["symbol"] == "AAA"
    assert data["bar"]["open_time"] == CLOSED_OPEN.isoformat()
    assert data["indicators"]["rsi_14"] is not None
    assert data["indicator_bar_open_time"] == data["bar"]["open_time"]
    assert data["day_levels"]["pivots"]["pivot"] is not None
    assert data["market"]["is_open"] is not None


def test_an_unresolved_symbol_is_a_404_rather_than_an_empty_answer() -> None:
    client, _ = client_for("AAA")

    response = client.get("/v1/market/symbol/ZZZ?interval=15m")

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "SYMBOL_UNRESOLVED"


def test_the_symbol_bundle_refuses_an_unknown_timeframe() -> None:
    client, _ = client_for("AAA")

    response = client.get("/v1/market/symbol/AAA?interval=9m")

    assert response.status_code == 400


# -- is the bar that just closed finished yet? -------------------------------- #


def test_the_bar_status_says_the_bar_is_ready_when_every_minute_has_landed() -> None:
    client, _ = client_for("AAA")

    data = client.get("/v1/market/bar-status?interval=15m").json()["data"]

    assert data["bar_open_time"] == CLOSED_OPEN.isoformat()
    assert data["interval_minutes"] == 15
    assert data["symbols"] == 1
    assert data["ready"] == 1
    assert data["building"] == 0
    assert data["complete"] is True


def test_the_bar_status_says_which_symbols_are_still_short() -> None:
    """A bar still being built is reported as building, not as ready.

    This is what lets an agent wake on the boundary and start the moment the
    book is done, instead of sleeping a number of seconds somebody guessed.
    """

    # 358 minutes in: the 15:00-15:15 bucket holds 13 of its 15 minutes.
    client, _ = client_for("AAA", count=358)

    data = client.get("/v1/market/bar-status?interval=15m").json()["data"]

    assert data["bar_open_time"] == CLOSED_OPEN.isoformat()
    assert data["building"] == 1
    assert data["ready"] == 0
    assert data["complete"] is False


def test_the_bar_status_refuses_an_unknown_timeframe() -> None:
    client, _ = client_for("AAA")

    response = client.get("/v1/market/bar-status?interval=9m")

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "TIMEFRAME_UNKNOWN"


# -- the wake signal --------------------------------------------------------- #


def test_the_closed_bar_event_names_the_candle_that_just_finished() -> None:
    # Half a minute after 15:15, the 15:00-15:15 candle has just closed.
    _, service = client_for("AAA", now=SESSION_DAY + timedelta(minutes=360, seconds=30))

    event = service.closed_event("15m")

    assert event is not None
    assert event["interval"] == "15m"
    assert event["bar_open_time"] == (SESSION_DAY + timedelta(minutes=345)).isoformat()
    assert event["close_time"] == (SESSION_DAY + timedelta(minutes=360)).isoformat()
    assert event["symbols"] == 1
    # The session travels with the announcement: whoever wakes on it has to
    # decide whether it may act, and a missing answer must not read as "closed".
    assert event["market"]["is_open"] is True
    assert event["market"]["exchange"] == "NSE"


def test_a_candle_that_closed_a_while_ago_is_no_longer_news() -> None:
    """The stream reports transitions; it is not a history lesson."""

    _, service = client_for("AAA")  # 15:23, eight minutes after the close

    assert service.closed_event("15m") is None


def test_no_event_is_announced_for_a_candle_from_another_session() -> None:
    """At 09:16 nothing has closed today, and Friday's bar is not news."""

    _, service = client_for("AAA", now=SESSION_DAY + timedelta(minutes=1))

    assert service.closed_event("15m") is None


def test_the_session_clock_route_carries_the_holiday_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module1 import service

    payload = {
        "is_open": False,
        "is_holiday": True,
        "holiday_name": "Test Holiday",
        "holidays_modelled": True,
        "next_open": "2026-09-22T09:15:00+05:30",
    }
    monkeypatch.setattr(service, "describe_session", lambda: payload)
    client, _ = client_for("AAA")

    data = client.get("/v1/market/session-clock").json()["data"]

    assert data == payload


def test_the_holiday_route_publishes_the_local_list() -> None:
    from common.holidays import Holiday, HolidayCalendar
    from module1 import session_clock

    session_clock.install_calendar(
        HolidayCalendar(
            [Holiday(date(2026, 9, 21), "Test Holiday")], source="test"
        )
    )
    client, _ = client_for("AAA")

    data = client.get("/v1/market/holidays").json()["data"]

    assert data["source"] == "test"
    assert data["holidays"] == [{"date": "2026-09-21", "name": "Test Holiday"}]
    assert data["years"] == [2026]

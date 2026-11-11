"""Screening and the per-symbol bundle: the two reads an agent lives on.

The screen has one job that matters more than the rest - it must describe the
candle that has *finished*, not the one being built - and the bundle has one
job that matters more than the rest - the bar and its indicator values must be
the same candle.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from module1.analytics import IndicatorService
from module1.broker.kite import Candle
from module1.builder import CandleBook
from module1.candles import CandleService, SymbolUnresolved, TimeframeError
from module1.timeutil import IST

pytestmark = pytest.mark.unit

SESSION_DAY = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
#: 15:23 IST: the 15:15-15:30 bucket is still being built.
MID_BUCKET = SESSION_DAY + timedelta(minutes=368)
#: 15:00, the last 15m bar that has finished.
CLOSED_OPEN = SESSION_DAY + timedelta(minutes=345)


class FakeClient:
    """No broker: the day series is canned, history is never asked for."""

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


def minutes(count: int) -> list[Candle]:
    """``count`` one-minute candles from 09:15, drifting gently upward."""

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


def meta(symbol: str) -> dict[str, Any]:
    return {
        "name": f"{symbol} Ltd.",
        "industry": "IT",
        "instrument_token": "1",
        "last_price": 103.67,
        "previous_close": 103.0,
        "day_change_percent": 0.65,
        "day_low": 99.5,
        "day_high": 104.2,
        "day_volume": 12345.0,
        "price_source": "live",
        "price_age_seconds": 3.0,
    }


def build(
    now: datetime | None = None,
) -> tuple[CandleService, CandleBook, IndicatorService]:
    moment = now or MID_BUCKET
    book = CandleBook()
    service = CandleService(
        FakeClient(),
        resolve_token=lambda symbol: "1",
        book=book,
        symbol_meta=meta,
        clock=lambda: moment,
    )
    indicators = IndicatorService(candles=service, book=book, clock=lambda: moment)
    service.set_indicator_provider(
        indicators.latest, indicators.describe, indicators.closed_frame
    )
    return service, book, indicators


def seeded(*symbols: str) -> tuple[CandleService, CandleBook, IndicatorService]:
    service, book, indicators = build()
    for symbol in symbols:
        book.seed(symbol, minutes(368))
    indicators.refresh_stale(limit=50)
    return service, book, indicators


# -- the screen -------------------------------------------------------------- #


def test_the_screen_describes_the_candle_that_finished() -> None:
    """Not the one still being built: a half-made bar is not a signal."""

    service, _, _ = seeded("AAA")

    result = service.screen("15m")

    assert result["bar_open_time"] == CLOSED_OPEN.isoformat()
    # The closed bar ends on minute 359 (15:14); the forming one on 367.
    assert result["rows"][0]["close"] == float(round(100 + 359 * 0.01, 2))
    assert result["rows"][0]["close"] != float(round(100 + 367 * 0.01, 2))


def test_every_screened_row_is_the_same_candle() -> None:
    """One filter may not compare two different bars."""

    service, _, _ = seeded("AAA", "BBB")

    result = service.screen("15m", where=[{"field": "close", "op": ">", "value": 0}])

    assert [row["symbol"] for row in result["rows"]] == ["AAA", "BBB"]
    assert result["bar_open_time"] == CLOSED_OPEN.isoformat()
    assert {row["close"] for row in result["rows"]} == {
        float(round(100 + 359 * 0.01, 2))
    }


def test_a_symbol_whose_last_candle_is_a_different_bar_is_left_out() -> None:
    """Silence would look like agreement, so the row is reported as missing."""

    service, book, indicators = build()
    book.seed("AAA", minutes(368))
    # BBB stopped trading at 14:59, so its last finished 15m bar is 14:45-15:00.
    book.seed("BBB", minutes(345))
    indicators.refresh_stale(limit=50)

    result = service.screen("15m")

    assert [row["symbol"] for row in result["rows"]] == ["AAA"]
    assert result["unavailable"] == ["BBB"]
    assert result["screened"] == 2


def test_a_field_with_no_history_behind_it_never_matches() -> None:
    """Twenty-four closed 15m bars cannot answer a thirty-bar question."""

    service, _, _ = seeded("AAA")

    deep = service.screen("15m", where=[{"field": "momentum_30", "op": ">", "value": 0}])
    shallow = service.screen("15m", where=[{"field": "momentum_5", "op": ">", "value": 0}])

    assert deep["matched"] == 0
    assert deep["rows"] == []
    assert shallow["matched"] == 1


def test_the_screen_carries_only_the_fields_it_was_asked_for() -> None:
    service, _, _ = seeded("AAA")

    result = service.screen(
        "15m", where=[{"field": "rsi_14", "op": ">", "value": 0}], fields=["rsi_14"]
    )

    assert set(result["rows"][0]) == {"symbol", "name", "industry", "rsi_14"}


def test_the_screen_says_what_it_was_asked() -> None:
    """A shortlist nobody can reproduce is not evidence."""

    service, _, _ = seeded("AAA")

    result = service.screen(
        "15m",
        where=[{"field": "volume_ratio", "op": ">=", "value": 1.0}],
        rank_by="volume_ratio",
        direction="desc",
        limit=3,
    )

    assert result["criteria"]["where"] == [
        {"field": "volume_ratio", "op": ">=", "value": 1.0}
    ]
    assert result["criteria"]["rank_by"] == "volume_ratio"
    assert result["criteria"]["direction"] == "desc"
    assert result["interval"] == "15m"
    assert result["market"]["is_open"] is not None


def test_an_unknown_timeframe_is_refused_before_anything_is_screened() -> None:
    service, _, _ = seeded("AAA")

    with pytest.raises(TimeframeError):
        service.screen("9m")


# -- the per-symbol bundle --------------------------------------------------- #


def test_the_bundle_answers_for_one_stock_on_one_timeframe() -> None:
    service, _, _ = seeded("AAA")

    bundle = service.symbol_bundle("AAA", "15m")

    assert bundle["symbol"] == "AAA"
    assert bundle["name"] == "AAA Ltd."
    assert bundle["industry"] == "IT"
    assert bundle["interval"] == "15m"
    assert bundle["bar"]["open_time"] == CLOSED_OPEN.isoformat()
    assert bundle["bar"]["is_forming"] is False
    assert bundle["bar"]["close"] == float(round(100 + 359 * 0.01, 2))


def test_the_bundle_bar_and_its_indicators_are_the_same_candle() -> None:
    """The one invariant that makes a decision checkable afterwards."""

    service, _, _ = seeded("AAA")

    bundle = service.symbol_bundle("AAA", "15m")

    assert bundle["indicator_bar_open_time"] == bundle["bar"]["open_time"]
    assert bundle["indicators"]["rsi_14"] is not None


def test_the_bundle_carries_the_levels_and_the_session() -> None:
    service, _, _ = seeded("AAA")

    bundle = service.symbol_bundle("AAA", "15m")

    assert bundle["day_levels"] is not None
    assert set(bundle["day_levels"]["pivots"]) == {
        "pivot",
        "r1",
        "r2",
        "r3",
        "s1",
        "s2",
        "s3",
    }
    assert bundle["nearest_level"] is not None
    assert bundle["day"]["last_price"] == 103.67
    assert bundle["day"]["price_source"] == "live"
    assert bundle["market"]["session_open_ist"] == "09:15"
    assert bundle["groups"]


def test_a_stock_whose_first_candle_is_still_forming_has_nothing_to_show() -> None:
    """At 09:20 the opening 15m bucket has not ended, so there is no bar yet."""

    service, book, indicators = build(SESSION_DAY + timedelta(minutes=5))
    book.seed("NEW", minutes(4))
    indicators.refresh_stale(limit=50)

    with pytest.raises(SymbolUnresolved):
        service.symbol_bundle("NEW", "15m")


def test_a_finished_but_short_bar_is_shown_as_short_rather_than_hidden() -> None:
    """Four minutes is a real candle, and it admits it is only four minutes."""

    service, book, indicators = build()
    book.seed("THIN", minutes(4))
    indicators.refresh_stale(limit=50)

    bundle = service.symbol_bundle("THIN", "15m")

    assert bundle["bar"]["minutes_present"] == 4
    assert bundle["bar"]["minutes_expected"] == 15
    assert bundle["bar"]["is_gappy"] is True


def test_the_bundle_refuses_a_symbol_it_cannot_resolve() -> None:
    service, _, _ = seeded("AAA")
    service._resolve = lambda symbol: None

    with pytest.raises(SymbolUnresolved):
        service.symbol_bundle("AAA", "15m")


def test_the_bundle_refuses_an_unknown_timeframe() -> None:
    service, _, _ = seeded("AAA")

    with pytest.raises(TimeframeError):
        service.symbol_bundle("AAA", "9m")

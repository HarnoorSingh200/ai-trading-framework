"""The derived-bar cache and the indicator refresh loop."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from module1.analytics import IndicatorService
from module1.broker.kite import Candle
from module1.builder import CandleBook
from module1.candles import CandleService
from module1.indicators import FIELDS, compute_latest
from module1.timeutil import IST

pytestmark = pytest.mark.unit

START = datetime(2026, 9, 24, 9, 15, tzinfo=IST)
SYMBOL = "TCS"


class FakeClient:
    def historical(self, *args, **kwargs):
        return []

    def day_candles(self, token, *, sessions, now=None, interactive=False):
        return [
            Candle(
                open_time=START - timedelta(days=1),
                open=Decimal("100"),
                high=Decimal("105.50"),
                low=Decimal("98.25"),
                close=Decimal("103.75"),
                volume=Decimal("1000"),
            )
        ]


def minutes(count: int = 400) -> list[Candle]:
    rows = []
    price = 100.0
    for index in range(count):
        drift = ((index * 37) % 11 - 5) * 0.35
        closed = price + drift
        rows.append(
            Candle(
                open_time=START + timedelta(minutes=index),
                open=Decimal(f"{price:.2f}"),
                high=Decimal(f"{max(price, closed) + 0.4:.2f}"),
                low=Decimal(f"{min(price, closed) - 0.35:.2f}"),
                close=Decimal(f"{closed:.2f}"),
                volume=Decimal(str(1000 + (index * 137) % 900)),
            )
        )
        price = closed
    return rows


def build(
    now: datetime | None = None,
) -> tuple[CandleService, CandleBook, IndicatorService]:
    moment = now or START + timedelta(minutes=400)
    book = CandleBook()
    service = CandleService(
        FakeClient(),
        resolve_token=lambda symbol: "1",
        book=book,
        symbol_meta=lambda symbol: {"name": "Test"},
        clock=lambda: moment,
    )
    indicators = IndicatorService(candles=service, book=book, clock=lambda: moment)
    service.set_indicator_provider(indicators.latest, indicators.describe)
    return service, book, indicators


def test_the_bars_are_aggregated_once_per_book_version() -> None:
    """The whole point of the cache: a read must not re-bucket the series."""

    service, book, _ = build()
    book.seed(SYMBOL, minutes())

    service.bars(SYMBOL, "15m")
    service.bars(SYMBOL, "15m")
    service.bars(SYMBOL, "15m")

    assert service.aggregations == 1
    assert service.cache_hits == 2

    # A new minute is a new version, so exactly one more aggregation.
    book.seed(SYMBOL, minutes(401))

    service.bars(SYMBOL, "15m")

    assert service.aggregations == 2


def test_a_read_serves_what_is_cached_rather_than_rebuilding() -> None:
    """A request must never pay for fifty symbols of re-aggregation."""

    service, book, _ = build()
    book.seed(SYMBOL, minutes())
    service.bars(SYMBOL, "15m")
    before = service.aggregations

    book.seed(SYMBOL, minutes(401))
    # The board path.
    service.bars(SYMBOL, "15m", refresh_stale=False)

    assert service.aggregations == before


def test_the_board_rows_carry_indicators_and_the_bar_they_belong_to() -> None:
    service, book, indicators = build()
    book.seed(SYMBOL, minutes())
    indicators.refresh_symbol(SYMBOL)

    board = service.timeframe_board("15m")
    row = board["rows"][0]

    assert board["indicator_version"] == "1.0.0"
    assert row["indicators"] is not None
    assert row["indicators"]["rsi_14"] is not None
    assert row["indicator_bar_open_time"] is not None


def test_only_the_changed_symbols_are_refreshed() -> None:
    service, book, indicators = build()
    book.seed(SYMBOL, minutes())
    book.seed("INFY", minutes())

    first = indicators.refresh_stale(limit=10)
    assert first["refreshed"] == 2
    assert indicators.describe()["stale_symbols"] == 0

    # Nothing changed, so nothing is stale.
    assert indicators.refresh_stale(limit=10)["refreshed"] == 0

    book.seed(SYMBOL, minutes(401))
    again = indicators.refresh_stale(limit=10)

    assert again["symbols"] == [SYMBOL]
    assert indicators.describe()["stale_symbols"] == 0


def test_pivot_levels_come_from_the_previous_session_only() -> None:
    service, book, indicators = build()
    book.seed(SYMBOL, minutes())

    levels = indicators.levels_for(SYMBOL)

    assert levels is not None
    assert float(levels.high) == 105.50
    assert levels.pivots()["pivot"] == Decimal("102.50")
    assert levels.session_date is not None


def test_the_catalogue_covers_every_published_field() -> None:
    """Field names are produced in one place; this keeps the list honest."""

    service, book, indicators = build()
    book.seed(SYMBOL, minutes())
    values = indicators.refresh_symbol(SYMBOL) and indicators.latest(SYMBOL, "15m")
    assert values is not None

    published = {spec.name for spec in FIELDS}
    # Everything the engine publishes is in the catalogue, the previous
    # session's levels included. They are marked ``surface="inspector"`` so they
    # stay out of the table's columns, but leaving them out of the catalogue
    # entirely made them impossible to ask for by name - and they were asked
    # for, repeatedly, by a model that could see the values and not the name.
    assert set(values["values"]) - published == set()


def test_a_timeframe_without_enough_history_reports_nulls() -> None:
    service, book, indicators = build()
    book.seed(SYMBOL, minutes(8))
    indicators.refresh_symbol(SYMBOL)

    latest = indicators.latest(SYMBOL, "15m")

    assert latest is not None
    assert latest["values"]["rsi_14"] is None
    assert latest["values"]["candle_colour"] in {"green", "red", "doji"}


#: 15:23 IST: inside the 15:15-15:30 bucket, so that bar is still forming.
MID_BUCKET = START + timedelta(minutes=368)
#: The 15:15 bucket that is being built, and the 15:00 one that just closed.
FORMING_OPEN = START + timedelta(minutes=360)
CLOSED_OPEN = START + timedelta(minutes=345)


def test_the_closed_frame_is_the_bar_before_the_forming_one() -> None:
    """A value that belongs to the bar being built cannot be the agent's input.

    The live frame follows the forming bar, which is what the table shows while
    a session runs. An agent waking on a bar close needs the bar *before* it,
    and the two must never be confused for one another.
    """

    service, book, indicators = build(MID_BUCKET)
    book.seed(SYMBOL, minutes(368))
    indicators.refresh_symbol(SYMBOL)

    live = indicators.latest(SYMBOL, "15m")
    closed = indicators.closed_latest(SYMBOL, "15m")

    assert live["bar_open_time"] == FORMING_OPEN.isoformat()
    assert closed is not None
    assert closed["bar_open_time"] == CLOSED_OPEN.isoformat()


def test_the_closed_frame_is_computed_from_the_closed_bar_alone() -> None:
    service, book, indicators = build(MID_BUCKET)
    book.seed(SYMBOL, minutes(368))
    indicators.refresh_symbol(SYMBOL)

    bars = service.bars(SYMBOL, "15m", now=MID_BUCKET)
    expected = compute_latest(
        [bar for bar in bars if not bar.is_forming],
        day_levels=indicators.levels_for(SYMBOL),
    )

    closed = indicators.closed_latest(SYMBOL, "15m")
    live = indicators.latest(SYMBOL, "15m")

    assert closed is not None and live is not None
    assert closed["values"] == expected
    # The two frames are different bars, so they are different numbers.
    assert live["values"]["change_pct"] != closed["values"]["change_pct"]


def test_the_closed_frame_is_the_live_one_when_nothing_is_forming() -> None:
    """After the close there is nothing to distinguish, so nothing is recomputed."""

    service, book, indicators = build()
    # A whole session, 09:15 to 15:29, read at 15:55: every bucket has ended.
    book.seed(SYMBOL, minutes(375))
    indicators.refresh_symbol(SYMBOL)

    assert indicators.closed_latest(SYMBOL, "15m") == indicators.latest(SYMBOL, "15m")


def test_the_closed_frame_can_be_served_with_its_own_levels() -> None:
    service, book, indicators = build(MID_BUCKET)
    book.seed(SYMBOL, minutes(368))
    indicators.refresh_symbol(SYMBOL)

    payload = indicators.frame(SYMBOL, "15m", closed=True)

    assert payload["bar_open_time"] == CLOSED_OPEN.isoformat()
    assert payload["day_levels"] is not None
    assert payload["values"] == indicators.closed_latest(SYMBOL, "15m")["values"]


def test_a_symbol_with_no_closed_bar_yet_reports_nothing_rather_than_the_forming_one() -> None:
    """The first minutes of a session must not look like a finished candle."""

    service, book, indicators = build(START + timedelta(minutes=5))
    book.seed(SYMBOL, minutes(5))
    indicators.refresh_symbol(SYMBOL)

    assert indicators.latest(SYMBOL, "15m") is not None
    assert indicators.closed_latest(SYMBOL, "15m") is None

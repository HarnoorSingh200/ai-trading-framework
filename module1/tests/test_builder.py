"""Candles built from ticks: the live edge, and the holes in it."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from module1.broker.feed import Tick
from module1.broker.kite import Candle
from module1.builder import CandleBook
from module1.timeutil import IST

pytestmark = pytest.mark.unit

SESSION_DAY = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
SYMBOL = "TCS"


def tick(
    offset: int,
    *,
    price: str = "100",
    volume: str | None = None,
    received_offset: int | None = None,
) -> Tick:
    """A tick whose exchange stamp is ``offset`` minutes into the session."""

    exchange = SESSION_DAY + timedelta(minutes=offset)
    received = SESSION_DAY + timedelta(
        minutes=offset if received_offset is None else received_offset
    )
    return Tick(
        instrument_token="1234",
        price=Decimal(price),
        received_at=received,
        exchange_time=exchange,
        volume=None if volume is None else Decimal(volume),
        symbol=SYMBOL,
    )


def minute(offset: int, *, close: str = "100", source: str = "broker") -> Candle:
    price = Decimal(close)
    return Candle(
        open_time=SESSION_DAY + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("500"),
        source=source,
    )


def test_the_first_tick_only_opens_a_bucket() -> None:
    book = CandleBook()

    assert book.on_tick(tick(0), SYMBOL) == []
    assert book.coverage(SYMBOL)["minutes"] == 0
    assert book.stats()["forming"] == 1


def test_the_next_minute_closes_the_previous_one() -> None:
    book = CandleBook()

    book.on_tick(tick(0, price="100"), SYMBOL)
    book.on_tick(tick(0, price="102"), SYMBOL)
    closed = book.on_tick(tick(1, price="101"), SYMBOL)

    assert len(closed) == 1
    candle = closed[0]
    assert candle.open_time == SESSION_DAY
    assert candle.open == Decimal("100")
    assert candle.high == Decimal("102")
    assert candle.close == Decimal("102")
    assert candle.source == "live"
    assert book.coverage(SYMBOL)["minutes"] == 1


def test_a_late_tick_belongs_to_its_exchange_minute() -> None:
    """The exchange clock decides the bucket, not the moment it reached us."""

    book = CandleBook()

    book.on_tick(tick(0, received_offset=5), SYMBOL)
    closed = book.on_tick(tick(1, received_offset=6), SYMBOL)

    assert closed[0].open_time == SESSION_DAY
    assert book.stats()["missing_minutes"] == 0


def test_an_out_of_order_tick_is_ignored() -> None:
    book = CandleBook()
    book.on_tick(tick(0, price="100"), SYMBOL)
    book.on_tick(tick(1, price="101"), SYMBOL)

    # A tick that arrives late, stamped for a minute already closed.
    assert book.on_tick(tick(0, price="999"), SYMBOL) == []
    assert book.minutes(SYMBOL)[0].close == Decimal("100")


def test_a_minute_with_no_tick_is_recorded_as_missing() -> None:
    book = CandleBook()

    book.on_tick(tick(0), SYMBOL)
    book.on_tick(tick(4), SYMBOL)
    book.on_tick(tick(5), SYMBOL)

    coverage = book.coverage(SYMBOL)
    # Nothing arrived for 09:16, 09:17 or 09:18.
    assert coverage["missing_minutes"] == 3
    assert book.pending_gaps()[0] == {"symbol": SYMBOL, "missing": 3}


def test_volume_is_a_difference_not_a_sum() -> None:
    """The stream reports cumulative volume, so a candle's volume is a delta."""

    book = CandleBook()

    book.on_tick(tick(0, volume="1000"), SYMBOL)
    book.on_tick(tick(0, volume="1300"), SYMBOL)
    closed = book.on_tick(tick(1, volume="1500"), SYMBOL)

    assert closed[0].volume == Decimal("300")


def test_seeding_history_is_authoritative_and_closes_the_gaps() -> None:
    book = CandleBook()
    book.on_tick(tick(0), SYMBOL)
    book.on_tick(tick(3), SYMBOL)
    assert book.coverage(SYMBOL)["missing_minutes"] == 2

    added = book.seed(SYMBOL, [minute(0, close="111"), minute(1), minute(2), minute(3)])

    # Three minutes are new; 09:15 already existed and is overwritten, not
    # added - which is the point, because history is the better record of it.
    assert added == 3
    coverage = book.coverage(SYMBOL)
    assert coverage["missing_minutes"] == 0
    assert coverage["minutes"] == 4
    # History replaced the locally built minute with the exchange's own record.
    assert book.minutes(SYMBOL)[0].close == Decimal("111")
    assert book.minutes(SYMBOL)[0].source == "broker"


def test_closing_a_candle_notifies_the_consumer() -> None:
    seen = []
    book = CandleBook(on_closed=seen.append)

    book.on_tick(tick(0, price="100"), SYMBOL)
    book.on_tick(tick(0, price="105"), SYMBOL)
    book.on_tick(tick(1, price="105"), SYMBOL)

    assert len(seen) == 1
    assert seen[0].symbol == SYMBOL
    assert seen[0].interval == "1m"
    assert seen[0].ticks == 2
    assert seen[0].candle.close == Decimal("105")


def test_a_broken_consumer_cannot_cost_us_the_candle() -> None:
    def explode(_closed):
        raise RuntimeError("consumer is broken")

    book = CandleBook(on_closed=explode)
    book.on_tick(tick(0), SYMBOL)

    closed = book.on_tick(tick(1), SYMBOL)

    assert len(closed) == 1
    assert book.coverage(SYMBOL)["minutes"] == 1


def test_the_window_stays_bounded() -> None:
    book = CandleBook(keep_minutes=120)

    for offset in range(200):
        book.on_tick(tick(offset), SYMBOL)
    book.close_symbol(SYMBOL)

    assert book.coverage(SYMBOL)["minutes"] <= 121


def test_close_all_finishes_every_open_bucket() -> None:
    book = CandleBook()
    book.on_tick(tick(0), SYMBOL)

    closed = book.close_all()

    assert len(closed) == 1
    assert book.stats()["forming"] == 0


def test_a_built_minute_stays_unconfirmed_until_history_covers_it() -> None:
    """Ticks sample the market; history records it. Repair closes the gap."""

    book = CandleBook()
    book.on_tick(tick(0), SYMBOL)
    book.on_tick(tick(1), SYMBOL)  # closes 09:15, built from ticks

    pending = book.pending_repairs(now=SESSION_DAY + timedelta(minutes=3), limit=5)

    assert pending == [{"symbol": SYMBOL, "missing": 0, "unconfirmed": 1}]

    # History is the exchange's record, and it settles the question.
    book.seed(SYMBOL, [minute(0)])

    assert book.pending_repairs(now=SESSION_DAY + timedelta(minutes=3)) == []
    # Only 09:15 has been closed so far; 09:16 is still forming.
    assert book.counted_sources(SYMBOL) == {"broker": 1}


def test_a_minute_that_just_closed_is_left_to_settle() -> None:
    """History runs about a minute behind, so asking immediately is wasted."""

    book = CandleBook()
    book.on_tick(tick(0), SYMBOL)
    book.on_tick(tick(1), SYMBOL)

    # Ten seconds after the minute closed, nothing has been published yet.
    pending = book.pending_repairs(
        now=SESSION_DAY + timedelta(minutes=1, seconds=10), settle_seconds=75
    )

    assert pending == []


def test_a_minute_too_old_to_be_republished_is_not_chased_for_ever() -> None:
    book = CandleBook()
    book.on_tick(tick(0), SYMBOL)
    book.on_tick(tick(1), SYMBOL)

    # Two hours later the window has passed, so no request is spent on it.
    pending = book.pending_repairs(now=SESSION_DAY + timedelta(hours=2), lookback_minutes=45)

    assert pending == []

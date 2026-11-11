"""The order model: what becomes an order, and which price ends it how.

These are the rules the whole module rests on, so they are tested against the
numbers rather than against the code: a stop is booked at the level or worse, a
target never better than the target, and a single price that clears both is
decided against the position and says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from module3.orders import (
    EXIT_STOP,
    EXIT_TARGET,
    EXIT_TIMEOUT,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_PENDING,
    Order,
    OrderError,
    close,
    evaluate,
    expire,
    fill,
    order_from_decision,
    plan_holds_at,
)
from module3.timeutil import IST

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=IST)
DECIDED_AT = NOW - timedelta(seconds=45)


def decision(**overrides: object) -> dict[str, object]:
    """A decision exactly as Module 2 records one."""

    payload: dict[str, object] = {
        "decision_id": "dec_abc123",
        "agent_id": "alpha",
        "action": "buy",
        "symbol": "tcs",
        "quantity": 96,
        "entry_price": 412.90,
        "target": 416.50,
        "stop_loss": 411.40,
        "max_hold_minutes": 60,
        "confidence": 0.6,
        "reason": "close above VWAP with volume_ratio 3.1",
        "timeframe": "15m",
        "bar_open_time": "2026-09-21T09:00:00+05:30",
        "model": "deepseek-v4-flash",
        "price_source": "live",
        "created_at": DECIDED_AT.isoformat(),
    }
    payload.update(overrides)
    return payload


def order(**overrides: object):
    return order_from_decision(decision(**overrides), now=NOW, order_id="ord_1")


def opened(*, price: str = "412.90", **overrides: object):
    return fill(order(**overrides), at=NOW, price=Decimal(price))


# -- intake ------------------------------------------------------------------ #


def test_a_decision_becomes_an_order_that_can_be_audited() -> None:
    built = order()

    assert built.status == STATUS_PENDING
    assert built.symbol == "TCS"  # normalised, because that is how it is quoted
    assert built.side == "buy"
    assert built.quantity == 96
    # Until it fills, the entry is the price the plan was built on.
    assert built.entry_price == Decimal("412.90")
    assert built.decision_entry_price == Decimal("412.90")
    assert built.notional == Decimal("39638.40")
    assert built.risk_per_share == Decimal("1.50")
    assert built.risk == Decimal("144.00")
    assert built.created_at == DECIDED_AT
    assert built.received_at == NOW
    assert built.decision_id == "dec_abc123"


def test_standing_aside_is_not_an_order() -> None:
    with pytest.raises(OrderError) as caught:
        order(action="ignore", target=None, stop_loss=None)

    assert caught.value.field == "action"
    assert "not a side" in str(caught.value)


def test_a_target_on_the_wrong_side_is_refused_with_the_field() -> None:
    with pytest.raises(OrderError) as caught:
        order(target=410.0)

    assert caught.value.field == "target"
    assert "below the entry" in str(caught.value)


def test_a_short_with_its_stop_below_the_entry_is_refused() -> None:
    with pytest.raises(OrderError) as caught:
        order(action="sell", target=410.0, stop_loss=411.0)

    assert caught.value.field == "target"
    assert "above it" in str(caught.value)


def test_a_valid_short_is_accepted() -> None:
    built = order(action="sell", target=408.0, stop_loss=415.0)

    assert built.side == "sell"
    assert built.risk_per_share == Decimal("2.10")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"reason": "   "}, "reason"),
        ({"quantity": 0}, "quantity"),
        ({"entry_price": None}, "entry_price"),
        ({"stop_loss": -1}, "stop_loss"),
        ({"max_hold_minutes": 0}, "max_hold_minutes"),
        ({"decision_id": ""}, "decision_id"),
        ({"agent_id": ""}, "agent_id"),
        ({"symbol": ""}, "symbol"),
    ],
)
def test_a_decision_that_cannot_be_an_order_names_the_field(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(OrderError) as caught:
        order(**overrides)

    assert caught.value.field == field


# -- transitions -------------------------------------------------------------- #


def test_filling_starts_the_clock_at_the_fill_not_at_the_decision() -> None:
    built = order()

    filled = fill(built, at=NOW, price=Decimal("413.30"))

    assert filled.status == STATUS_OPEN
    assert filled.filled_at == NOW
    # The entry is the price the market was publishing, and the plan's own price
    # stays beside it so the difference between the two is readable.
    assert filled.entry_price == Decimal("413.30")
    assert filled.decision_entry_price == Decimal("412.90")
    assert filled.deadline == NOW + timedelta(minutes=60)


def test_an_order_cannot_be_filled_twice() -> None:
    with pytest.raises(OrderError) as caught:
        fill(opened(), at=NOW, price=Decimal("412.90"))

    assert caught.value.field == "status"


def test_only_an_open_order_can_close() -> None:
    exit_ = evaluate(opened(), price=Decimal("416.60"), at=NOW, now=NOW)
    assert exit_ is not None

    with pytest.raises(OrderError):
        close(order(), exit_, at=NOW)


def test_an_unfilled_order_expires_rather_than_waiting_for_ever() -> None:
    expired = expire(order(), at=NOW, note="no live price arrived")

    assert expired.status == "expired"
    assert expired.ended_at == NOW
    assert expired.exit_note == "no live price arrived"


def test_an_order_that_is_not_open_produces_no_exit() -> None:
    assert evaluate(order(), price=Decimal("500"), at=NOW, now=NOW) is None


# -- exits -------------------------------------------------------------------- #


def test_the_target_ends_a_buy_at_the_target() -> None:
    exit_ = evaluate(opened(), price=Decimal("417.10"), at=NOW, now=NOW)

    assert exit_ is not None
    assert exit_.reason == EXIT_TARGET
    # A gap through the target is real, but which side of it a fill landed on
    # is not knowable from a snapshot, so it is never booked better than plan.
    assert exit_.price == Decimal("416.50")

    closed = close(opened(), exit_, at=NOW)
    assert closed.status == STATUS_CLOSED
    assert closed.exit_reason == EXIT_TARGET
    assert closed.pnl is not None
    assert closed.pnl.gross == Decimal("345.60")  # (416.50 - 412.90) * 96


def test_the_stop_ends_a_buy_at_the_level_or_worse() -> None:
    exit_ = evaluate(opened(), price=Decimal("410.10"), at=NOW, now=NOW)

    assert exit_ is not None
    assert exit_.reason == EXIT_STOP
    assert exit_.price == Decimal("410.10")  # worse than the 411.40 stop, and real

    closed = close(opened(), exit_, at=NOW)
    assert closed.pnl is not None
    assert closed.pnl.gross == Decimal("-268.80")  # (410.10 - 412.90) * 96


def test_a_short_is_stopped_out_by_a_price_above_its_stop() -> None:
    short = opened(action="sell", target=408.0, stop_loss=415.0)

    exit_ = evaluate(short, price=Decimal("416.20"), at=NOW, now=NOW)

    assert exit_ is not None
    assert exit_.reason == EXIT_STOP
    assert exit_.price == Decimal("416.20")  # worse than 415.00 for a short
    settled = close(short, exit_, at=NOW)
    assert settled.pnl is not None
    assert settled.pnl.gross == Decimal("-316.80")


def test_a_price_that_clears_both_levels_is_decided_against_the_position() -> None:
    """One snapshot cannot say which came first, so the worse one counts."""

    target_first = evaluate(
        opened(),
        price=Decimal("417.00"),
        at=NOW,
        now=NOW,
        previous_price=Decimal("411.00"),  # was below the stop, is now above the target
    )

    assert target_first is not None
    assert target_first.reason == EXIT_STOP
    assert target_first.ambiguous is True
    assert target_first.note is not None
    assert "no honest way" in target_first.note

    closed = close(opened(), target_first, at=NOW)
    assert closed.ambiguous_exit is True
    assert closed.exit_reason == EXIT_STOP


def test_a_move_that_only_reaches_the_target_is_not_ambiguous() -> None:
    exit_ = evaluate(
        opened(),
        price=Decimal("416.60"),
        at=NOW,
        now=NOW,
        previous_price=Decimal("413.00"),
    )

    assert exit_ is not None
    assert exit_.reason == EXIT_TARGET
    assert exit_.ambiguous is False


def test_the_clock_ends_an_order_that_never_reaches_a_level() -> None:
    held = opened()
    deadline = held.deadline
    assert deadline is not None

    exit_ = evaluate(held, price=Decimal("413.10"), at=deadline, now=deadline)

    assert exit_ is not None
    assert exit_.reason == EXIT_TIMEOUT
    assert exit_.price == Decimal("413.10")
    assert exit_.note is None


def test_a_price_seen_after_the_deadline_cannot_be_booked_as_a_target() -> None:
    """The order was not in the market any more when that price printed."""

    held = opened()
    deadline = held.deadline
    assert deadline is not None
    late = deadline + timedelta(minutes=2)

    exit_ = evaluate(held, price=Decimal("420.00"), at=late, now=late)

    assert exit_ is not None
    assert exit_.reason == EXIT_TIMEOUT
    assert exit_.note is not None
    assert "after the holding deadline" in exit_.note


def test_a_level_is_still_honoured_when_it_was_touched_inside_the_window() -> None:
    """A stop seen inside the window counts, even if it is noticed afterwards."""

    held = opened()
    deadline = held.deadline
    assert deadline is not None

    exit_ = evaluate(
        held,
        price=Decimal("411.00"),
        at=deadline - timedelta(seconds=5),
        now=deadline + timedelta(minutes=3),
    )

    assert exit_ is not None
    assert exit_.reason == EXIT_STOP


# -- persistence round trip --------------------------------------------------- #


def test_an_order_survives_being_written_and_read_back() -> None:
    held = opened()
    exit_ = evaluate(held, price=Decimal("416.80"), at=NOW, now=NOW)
    assert exit_ is not None
    closed = close(held, exit_, at=NOW + timedelta(minutes=12))

    restored = Order.from_dict(closed.describe())

    assert restored.describe() == closed.describe()
    assert restored.pnl == closed.pnl
    assert restored.deadline == closed.deadline


def test_an_order_written_before_the_day_field_existed_still_gets_its_day() -> None:
    """``session_date`` was added later, and a book of undated rows reports
    nothing for them: the daily index comes back empty."""

    payload = opened().describe()
    payload.pop("session_date")

    restored = Order.from_dict(payload)

    assert restored.session_date == NOW.date().isoformat()


def test_an_order_with_no_timestamp_at_all_is_left_undated_rather_than_guessed() -> None:
    payload = opened().describe()
    payload.pop("session_date")
    payload.pop("received_at")
    payload.pop("created_at")
    payload.pop("filled_at")

    assert Order.from_dict(payload).session_date is None


# -- the plan has to still fit the market ------------------------------------- #


@pytest.mark.parametrize(
    ("price", "holds"),
    [
        ("411.39", False),  # below the stop
        ("411.40", False),  # exactly at the stop: stopped out on entry
        ("412.00", True),
        ("416.49", True),
        ("416.50", False),  # exactly at the target: no room left
        ("420.00", False),
    ],
)
def test_a_buy_plan_only_holds_between_its_stop_and_its_target(
    price: str, holds: bool
) -> None:
    assert plan_holds_at(order(), Decimal(price)) is holds


@pytest.mark.parametrize(
    ("price", "holds"),
    [
        ("420.01", False),
        ("420.00", False),
        ("419.00", True),
        ("405.01", True),
        ("405.00", False),
    ],
)
def test_a_short_plan_only_holds_between_its_target_and_its_stop(
    price: str, holds: bool
) -> None:
    short = order(action="sell", target=405.00, stop_loss=420.00)

    assert plan_holds_at(short, Decimal(price)) is holds


# -- the settlement ----------------------------------------------------------- #


def test_a_finished_order_carries_its_own_pnl() -> None:
    exit_ = evaluate(opened(), price=Decimal("417.10"), at=NOW, now=NOW)
    assert exit_ is not None

    closed = close(opened(), exit_, at=NOW)

    assert closed.pnl is not None
    assert closed.pnl.gross == Decimal("345.60")
    assert closed.pnl.capital == Decimal("39638.40")
    assert closed.pnl.risk == Decimal("144.00")
    assert closed.pnl.planned_reward == Decimal("345.60")
    assert closed.pnl.r_multiple == Decimal("2.4000")
    assert closed.pnl.reward_risk == Decimal("2.4000")
    assert closed.pnl.percent == Decimal("0.8719")
    assert closed.pnl.outcome == "win"


def test_a_stopped_out_order_settles_as_a_loss() -> None:
    exit_ = evaluate(opened(), price=Decimal("410.10"), at=NOW, now=NOW)
    assert exit_ is not None

    closed = close(opened(), exit_, at=NOW)

    assert closed.pnl is not None
    assert closed.pnl.outcome == "loss"
    assert closed.pnl.gross == Decimal("-268.80")
    assert closed.pnl.r_multiple == Decimal("-1.8667")


def test_the_pnl_is_measured_from_the_entry_the_order_actually_got() -> None:
    """The plan implied 345.60; the position, entered 40 paise higher, made less."""

    held = opened(price="413.30")
    assert held.entry_price == Decimal("413.30")
    assert held.decision_entry_price == Decimal("412.90")

    exit_ = evaluate(held, price=Decimal("417.10"), at=NOW, now=NOW)
    assert exit_ is not None
    settled = close(held, exit_, at=NOW)

    assert settled.pnl is not None
    assert settled.pnl.gross == Decimal("307.20")  # (416.50 - 413.30) * 96
    assert settled.pnl.risk == Decimal("182.40")  # (413.30 - 411.40) * 96
    assert settled.pnl.r_multiple == Decimal("1.6842")


def test_an_order_that_never_filled_has_no_pnl_to_report() -> None:
    """Nothing was traded, so there is nothing to total."""

    expired = expire(order(), at=NOW, note="no live price arrived")

    assert expired.pnl is None

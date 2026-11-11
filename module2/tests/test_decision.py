"""The two things that stand between a model's opinion and an order: the rules
it must satisfy, and the arithmetic that turns a stop into a quantity."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from module2.decision import (
    REASON_WORD_LIMIT,
    DecisionError,
    Problem,
    check,
    proposal_from_args,
    size,
)
from module2.definition import AgentDefinition
from module2.timeutil import IST

pytestmark = pytest.mark.unit

#: 10:45 IST on a Monday, half an hour before the last actionable bar.
NOW = datetime(2026, 9, 21, 10, 45, tzinfo=IST)


def definition(**overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": "alpha",
        "name": "Alpha",
        "system_prompt": "Trade the 15-minute.",
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def bundle(
    *,
    price: float = 100.0,
    source: str = "live",
    age: float | None = 3.0,
    symbol: str = "INFY",
) -> dict:
    return {
        "symbol": symbol,
        "bar": {"open_time": "2026-09-21T10:30:00+05:30", "close": price},
        "day": {
            "last_price": price,
            "price_source": source,
            "price_age_seconds": age,
        },
    }


def rules(problems: tuple[Problem, ...]) -> set[str]:
    return {problem.rule for problem in problems}


# -- sizing ------------------------------------------------------------------ #


def test_the_quantity_is_the_smaller_of_what_the_risk_allows_and_what_the_capital_allows() -> None:
    """50,000 at 4x risking 0.5%: 250 rupees of risk, 40,000 of notional."""

    quantity, details = size(entry=Decimal("100"), stop=Decimal("98"), definition=definition())

    # 250 / 2 = 125 shares by risk; 40,000 / 100 = 400 by notional. Risk wins.
    assert quantity == 125
    assert details["by_risk"] == 125
    assert details["by_notional"] == 400
    assert details["risk_per_share"] == "2"
    assert details["money_at_risk"] == "250.00"


def test_a_tight_stop_is_capped_by_the_notional_limit_not_the_risk() -> None:
    quantity, details = size(
        entry=Decimal("100"), stop=Decimal("99.9"), definition=definition()
    )

    assert quantity == 400
    assert details["by_risk"] == 2500
    assert details["by_notional"] == 400


def test_a_price_that_cannot_be_split_into_one_share_is_no_position() -> None:
    """Better to say no than to round a position up past the cap."""

    quantity, details = size(
        entry=Decimal("100000"), stop=Decimal("99999"), definition=definition()
    )

    assert quantity == 0
    assert details["by_notional"] == 0


def test_a_stop_on_top_of_the_entry_is_refused_by_the_sizing_itself() -> None:
    with pytest.raises(DecisionError):
        size(entry=Decimal("100"), stop=Decimal("100"), definition=definition())


# -- the rules --------------------------------------------------------------- #


def test_a_well_formed_buy_passes_and_comes_back_sized() -> None:
    proposal = proposal_from_args(
        {
            "action": "buy",
            "symbol": "INFY",
            "target": 103.0,
            "stop_loss": 99.0,
            "max_hold_minutes": 45,
            "confidence": 0.6,
            "reason": "reclaim of VWAP with volume",
        }
    )

    checked = check(
        proposal, definition=definition(), bundle=bundle(), now=NOW, session_open=True
    )

    assert checked.ok is True
    # A one-rupee stop risks 250 rupees at 250 shares, and 250 shares of a
    # hundred-rupee stock is 25,000 - inside the 40,000 notional cap.
    assert checked.quantity == 250
    assert checked.entry_price == Decimal("100")


def test_a_target_on_the_wrong_side_of_the_entry_is_refused() -> None:
    proposal = proposal_from_args(
        {"action": "buy", "symbol": "INFY", "target": 99.0, "stop_loss": 98.0, "reason": "x"}
    )

    checked = check(
        proposal, definition=definition(), bundle=bundle(), now=NOW, session_open=True
    )

    assert checked.ok is False
    assert "target_and_stop" in rules(checked.problems)


def test_a_short_is_refused_when_the_agent_may_not_short() -> None:
    proposal = proposal_from_args(
        {"action": "sell", "symbol": "INFY", "target": 96.0, "stop_loss": 101.0, "reason": "x"}
    )

    checked = check(
        proposal,
        definition=definition(allow_short=False),
        bundle=bundle(),
        now=NOW,
        session_open=True,
    )

    assert checked.ok is False
    assert "short_not_allowed" in rules(checked.problems)


def test_a_short_passes_when_it_is_allowed_and_the_prices_straddle_the_market() -> None:
    proposal = proposal_from_args(
        {"action": "sell", "symbol": "INFY", "target": 96.0, "stop_loss": 101.0, "reason": "x"}
    )

    checked = check(
        proposal, definition=definition(), bundle=bundle(), now=NOW, session_open=True
    )

    assert checked.ok is True
    assert checked.quantity > 0


def test_a_holding_time_longer_than_the_agent_allows_is_refused() -> None:
    proposal = proposal_from_args(
        {
            "action": "buy",
            "symbol": "INFY",
            "target": 103.0,
            "stop_loss": 99.0,
            "max_hold_minutes": 90,
            "reason": "x",
        }
    )

    checked = check(
        proposal,
        definition=definition(max_hold_minutes=60),
        bundle=bundle(),
        now=NOW,
        session_open=True,
    )

    assert checked.ok is False
    assert "hold" in rules(checked.problems)


def test_an_exit_that_would_land_after_the_close_is_refused() -> None:
    """Intraday means squared off inside the session, not after it."""

    late = datetime(2026, 9, 21, 15, 20, tzinfo=IST)
    proposal = proposal_from_args(
        {
            "action": "buy",
            "symbol": "INFY",
            "target": 103.0,
            "stop_loss": 99.0,
            "max_hold_minutes": 30,
            "reason": "x",
        }
    )

    checked = check(
        proposal, definition=definition(), bundle=bundle(), now=late, session_open=True
    )

    assert checked.ok is False
    assert "exit_time" in rules(checked.problems)


def test_an_order_outside_the_session_is_refused() -> None:
    proposal = proposal_from_args(
        {"action": "buy", "symbol": "INFY", "target": 103.0, "stop_loss": 99.0, "reason": "x"}
    )

    checked = check(
        proposal, definition=definition(), bundle=bundle(), now=NOW, session_open=False
    )

    assert checked.ok is False
    assert "market_closed" in rules(checked.problems)


def test_a_price_that_is_not_live_is_not_a_price_to_size_on() -> None:
    proposal = proposal_from_args(
        {"action": "buy", "symbol": "INFY", "target": 103.0, "stop_loss": 99.0, "reason": "x"}
    )

    stale = check(
        proposal,
        definition=definition(),
        bundle=bundle(age=600.0),
        now=NOW,
        session_open=True,
    )
    missing = check(
        proposal,
        definition=definition(),
        bundle=bundle(source="none"),
        now=NOW,
        session_open=True,
    )

    assert stale.ok is False and "price" in rules(stale.problems)
    assert missing.ok is False and "price" in rules(missing.problems)


def test_a_stock_outside_the_universe_is_refused() -> None:
    proposal = proposal_from_args(
        {"action": "buy", "symbol": "NOTREAL", "target": 103.0, "stop_loss": 99.0, "reason": "x"}
    )

    checked = check(
        proposal, definition=definition(), bundle=None, now=NOW, session_open=True
    )

    assert checked.ok is False
    assert "symbol" in rules(checked.problems)


def test_an_order_without_a_reason_is_refused() -> None:
    """A decision nobody can explain afterwards is not a decision."""

    proposal = proposal_from_args(
        {"action": "buy", "symbol": "INFY", "target": 103.0, "stop_loss": 99.0, "reason": "  "}
    )

    checked = check(
        proposal, definition=definition(), bundle=bundle(), now=NOW, session_open=True
    )

    assert checked.ok is False
    assert "reason" in rules(checked.problems)


def test_ignoring_the_candle_needs_no_prices_and_passes() -> None:
    proposal = proposal_from_args({"action": "ignore", "reason": "no setup worth taking"})

    checked = check(
        proposal, definition=definition(), bundle=None, now=NOW, session_open=True
    )

    assert checked.ok is True
    assert checked.quantity == 0
    assert checked.entry_price is None


def test_an_action_that_is_not_one_of_the_three_is_refused() -> None:
    with pytest.raises(DecisionError):
        proposal_from_args({"action": "hold", "reason": "x"})


def test_a_missing_reason_is_refused_when_the_arguments_are_read() -> None:
    with pytest.raises(DecisionError):
        proposal_from_args({"action": "ignore"})


def test_a_confidence_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(DecisionError):
        proposal_from_args({"action": "ignore", "reason": "x", "confidence": 1.4})


def test_the_written_reason_is_capped_at_fifty_words() -> None:
    """The reason is the audit note; a note nobody reads to the end is not one."""

    allowed = {
        "action": "ignore",
        "reason": " ".join(["word"] * REASON_WORD_LIMIT),
    }
    assert len(proposal_from_args(allowed).reason.split()) == REASON_WORD_LIMIT

    over = {
        "action": "ignore",
        "reason": " ".join(["word"] * (REASON_WORD_LIMIT + 1)),
    }
    with pytest.raises(DecisionError) as failure:
        proposal_from_args(over)

    assert str(REASON_WORD_LIMIT) in str(failure.value)
    assert "reason" in str(failure.value).lower()

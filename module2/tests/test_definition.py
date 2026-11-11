"""What an agent is: the defaults, the bounds, and what is refused."""

from __future__ import annotations

from decimal import Decimal

import pytest

from module2.definition import AgentDefinition, DefinitionError

pytestmark = pytest.mark.unit


def definition(**overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": "momentum-alpha",
        "name": "Momentum alpha",
        "system_prompt": "You trade 15-minute momentum on the NIFTY 50.",
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def test_the_defaults_are_the_ones_the_brief_locked() -> None:
    agent = definition()

    assert agent.timeframe == "15m"
    assert agent.capital == Decimal("50000")
    assert agent.leverage == Decimal("4")
    assert agent.buying_power == Decimal("200000")
    assert agent.risk_per_trade == Decimal("250.00")
    assert agent.max_notional == Decimal("40000.00")
    assert agent.allow_short is True
    assert agent.max_hold_minutes == 60
    assert agent.context_timeframes == ()


def test_an_agent_round_trips_through_its_own_description() -> None:
    agent = definition(context_timeframes=["1d"], risk_per_trade_pct="0.75")

    again = AgentDefinition.from_dict(agent.describe())

    assert again == agent


def test_a_gpt_model_name_is_stored_as_written() -> None:
    agent = definition(model="gpt-6-luna")

    assert agent.model == "gpt-6-luna"


def test_an_agent_may_look_at_its_own_timeframe_and_the_ones_it_was_given() -> None:
    agent = definition(timeframe="15m", context_timeframes=["1d", "60m"])

    assert agent.allowed_intervals == frozenset({"15m", "1d", "60m"})


def test_the_definition_hash_changes_when_the_prompt_does() -> None:
    first = definition()
    same = definition()
    changed = definition(system_prompt="Something else entirely.")

    assert first.definition_hash() == same.definition_hash()
    assert first.definition_hash() != changed.definition_hash()


@pytest.mark.parametrize(
    "agent_id",
    ["Momentum", "has space", "ab", "-leading", "trailing-", "money$", ""],
)
def test_an_agent_id_that_is_not_a_usable_name_is_refused(agent_id: str) -> None:
    with pytest.raises(DefinitionError):
        definition(agent_id=agent_id)


def test_a_blank_system_prompt_is_refused() -> None:
    """An agent without instructions is an agent with nobody's instructions."""

    with pytest.raises(DefinitionError):
        definition(system_prompt="   ")


def test_an_unknown_timeframe_is_refused() -> None:
    with pytest.raises(DefinitionError) as failure:
        definition(timeframe="9m")

    assert "9m" in str(failure.value)


def test_looking_at_your_own_timeframe_as_context_is_refused() -> None:
    with pytest.raises(DefinitionError):
        definition(timeframe="15m", context_timeframes=["15m"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("leverage", 0),
        ("leverage", 101),
        ("capital", -1),
        ("risk_per_trade_pct", -0.1),
        ("max_capital_per_trade_pct", 101),
        ("max_hold_minutes", 0),
        ("max_hold_minutes", 400),
        ("max_tool_calls_per_cycle", 0),
        ("max_tool_rounds", 0),
        ("cycle_timeout_seconds", 0),
        ("temperature", 3),
    ],
)
def test_a_setting_outside_its_bounds_is_refused(field: str, value: object) -> None:
    with pytest.raises(DefinitionError):
        definition(**{field: value})


@pytest.mark.parametrize("value", ["plenty", "", None, True])
def test_a_number_that_is_not_a_number_is_refused(value: object) -> None:
    """``True`` is not one share: booleans are refused as quantities."""

    with pytest.raises(DefinitionError):
        definition(capital=value)


def test_an_unknown_setting_is_refused_rather_than_ignored() -> None:
    """A typo in a field name must fail loudly, not silently do nothing."""

    with pytest.raises(DefinitionError):
        definition(max_hold_minute=30)


def test_an_agent_is_enabled_unless_it_says_otherwise() -> None:
    """Off in production is a switch, not a delete: the definition stays."""

    assert definition().enabled is True
    assert definition(enabled=False).enabled is False
    assert AgentDefinition.from_dict(definition(enabled=False).describe()).enabled is False


def test_a_reasoning_effort_is_optional_and_validated() -> None:
    assert definition().reasoning_effort is None
    assert definition(reasoning_effort="high").reasoning_effort == "high"

    with pytest.raises(DefinitionError) as failure:
        definition(reasoning_effort="ultra")

    assert "reasoning_effort" in str(failure.value)

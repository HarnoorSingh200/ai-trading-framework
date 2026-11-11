"""The prompt: fixed enough to be cached, honest enough to decide on."""

from __future__ import annotations

from datetime import datetime

import pytest

from module2.definition import AgentDefinition
from module2.prompt import build_messages, system_message, tool_schemas
from module2.timeutil import IST

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 45, tzinfo=IST)


def definition(**overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": "alpha",
        "name": "Alpha",
        "system_prompt": "Buy strength, never chase.",
        "timeframe": "15m",
        "capital": "50000",
        "leverage": "4",
        "risk_per_trade_pct": "0.5",
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def test_the_system_message_is_byte_identical_between_cycles() -> None:
    """A stable prefix is a cached prefix, and a cached prefix is a cheap cycle."""

    agent = definition()

    assert system_message(agent) == system_message(agent)
    assert system_message(agent) == system_message(definition())


def test_the_system_message_carries_the_agent_own_words_verbatim() -> None:
    agent = definition(system_prompt="Only trade when the sector agrees with you.")

    text = system_message(agent)

    assert "Only trade when the sector agrees with you." in text


def test_the_system_message_states_the_frame_the_agent_must_work_inside() -> None:
    text = system_message(definition())

    assert "15m" in text
    # The numbers it is bounded by, so it is not guessing at its own size.
    assert "50000" in text or "50,000" in text
    assert "4" in text
    assert "0.5" in text
    # And the two rules that are not negotiable.
    assert "submit_decision" in text
    assert "closed" in text.lower()


def test_the_system_message_states_the_close_rules_without_a_second_version() -> None:
    text = system_message(definition())

    assert "No new order is accepted after the 14:30 decision" in text
    assert "flat by 14:50 IST" in text
    assert "closed automatically" in text
    assert "15:29" not in text


def test_the_tools_are_offered_in_a_stable_order() -> None:
    first = [schema["function"]["name"] for schema in tool_schemas(definition())]
    second = [schema["function"]["name"] for schema in tool_schemas(definition())]

    assert first == second
    assert first[-1] == "submit_decision"


def test_the_screen_tool_defaults_to_the_agent_own_timeframe() -> None:
    """The default is the definition, so the model need not remember it."""

    schemas = {schema["function"]["name"]: schema for schema in tool_schemas(definition())}
    interval = schemas["screen"]["function"]["parameters"]["properties"]["interval"]

    assert interval["default"] == "15m"


def test_the_decision_tool_offers_no_quantity() -> None:
    """Sizing is arithmetic, and arithmetic is not the model's job."""

    schemas = {schema["function"]["name"]: schema for schema in tool_schemas(definition())}
    properties = schemas["submit_decision"]["function"]["parameters"]["properties"]

    assert "quantity" not in properties
    assert set(schemas["submit_decision"]["function"]["parameters"]["required"]) == {
        "action",
        "reason",
    }


def test_the_volatile_part_is_last_and_changes_with_the_candle() -> None:
    agent = definition()
    first = build_messages(agent, bar_open_time="2026-09-21T10:30:00+05:30", now=NOW)
    second = build_messages(agent, bar_open_time="2026-09-21T10:45:00+05:30", now=NOW)

    assert [message["role"] for message in first] == ["system", "user"]
    assert [message["role"] for message in second] == ["system", "user"]
    # The system half is what gets cached; only the user half moves.
    assert first[0] == second[0]
    assert first[1] != second[1]
    assert "10:45" in second[1]["content"]


def test_the_system_message_gives_the_agent_a_notebook_and_says_it_is_mandatory() -> None:
    text = system_message(definition())

    assert "notebook" in text.lower()
    assert "mandatory" in text.lower()
    assert "delete what stopped being true" in text


def test_the_notebook_travels_in_the_volatile_half() -> None:
    """It changes when the agent writes, so it must not sit in the cached half."""

    agent = definition()
    digest = "Your notebook (persistent across every cycle and session):\n- orl: breaks failed"

    with_notes = build_messages(
        agent,
        bar_open_time="2026-09-21T10:30:00+05:30",
        now=NOW,
        notes=digest,
    )
    without = build_messages(agent, bar_open_time="2026-09-21T10:30:00+05:30", now=NOW)

    assert with_notes[0] == without[0]
    assert "persistent across every cycle" in with_notes[1]["content"]
    assert "persistent across every cycle" not in without[1]["content"]


def test_the_final_cycle_asks_for_a_whole_day_lesson() -> None:
    messages = build_messages(
        definition(),
        bar_open_time="2026-09-21T14:15:00+05:30",
        now=NOW,
        end_of_day=True,
    )

    content = messages[1]["content"]
    assert "final decision cycle" in content
    assert "whole-day lesson" in content
    assert "tomorrow" in content


def test_the_frame_puts_a_written_limit_on_the_reason() -> None:
    text = system_message(definition())

    assert "at most 50" in text
    assert "refused" in text

"""The registry and the two append-only logs."""

from __future__ import annotations

from pathlib import Path

import pytest

from module2.definition import AgentDefinition
from module2.store import AgentStore

pytestmark = pytest.mark.unit


def definition(agent_id: str = "alpha", **overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": agent_id,
        "name": f"Agent {agent_id}",
        "system_prompt": "Decide on closed candles only.",
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def test_an_agent_survives_a_restart(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    store.save(definition())

    again = AgentStore(tmp_path).load("alpha")

    assert again is not None
    assert again.agent_id == "alpha"
    assert again.system_prompt == "Decide on closed candles only."


def test_the_registry_lists_what_is_defined_and_nothing_else(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    store.save(definition("zeta"))
    store.save(definition("alpha"))

    assert [item.agent_id for item in store.list_definitions()] == ["alpha", "zeta"]
    assert store.load("nobody") is None
    assert store.exists("alpha") is True
    assert store.exists("nobody") is False


def test_editing_an_agent_replaces_its_definition(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    store.save(definition())
    store.save(definition(system_prompt="A sharper instruction."))

    loaded = store.load("alpha")

    assert loaded is not None
    assert loaded.system_prompt == "A sharper instruction."
    assert len(store.list_definitions()) == 1


def test_retiring_an_agent_keeps_its_history(tmp_path: Path) -> None:
    """Deleting an agent must not delete what it did."""

    store = AgentStore(tmp_path)
    store.save(definition())
    store.append_decision("alpha", {"action": "ignore", "reason": "nothing to do"})

    retired = store.retire("alpha")

    assert retired is not None and retired.exists()
    assert store.exists("alpha") is False
    assert store.list_definitions() == []
    kept = list((tmp_path / "_retired").rglob("decisions.jsonl"))
    assert len(kept) == 1
    assert "nothing to do" in kept[0].read_text(encoding="utf-8")


def test_retiring_an_agent_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    assert AgentStore(tmp_path).retire("ghost") is None


def test_activity_is_appended_with_a_time_and_read_back_newest_last(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    store.append_activity("alpha", {"type": "cycle_started", "cycle_id": "c1"})
    stored = store.append_activity("alpha", {"type": "tool_call", "tool": "screen"})

    events = store.read_activity("alpha")

    assert "ts" in stored
    assert [event["type"] for event in events] == ["cycle_started", "tool_call"]
    assert events[1]["tool"] == "screen"


def test_reading_a_log_returns_the_tail_it_was_asked_for(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    for index in range(10):
        store.append_activity("alpha", {"type": "tick", "index": index})

    tail = store.read_activity("alpha", limit=3)

    assert [event["index"] for event in tail] == [7, 8, 9]


def test_a_half_written_line_is_skipped_rather_than_raising(tmp_path: Path) -> None:
    """A log is read while it is being written; a torn tail is not a crisis."""

    store = AgentStore(tmp_path)
    store.append_activity("alpha", {"type": "cycle_started"})
    with store.activity_path("alpha").open("a", encoding="utf-8") as handle:
        handle.write('{"type": "half')

    events = store.read_activity("alpha")

    assert [event["type"] for event in events] == ["cycle_started"]


def test_the_state_survives_and_a_missing_one_is_empty(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    store.save(definition())

    assert store.load_state("alpha") == {}

    store.save_state("alpha", {"last_attempted_bar": "2026-09-27T10:30:00+05:30"})

    assert store.load_state("alpha")["last_attempted_bar"] == "2026-09-27T10:30:00+05:30"


def test_reading_a_log_for_an_agent_that_has_none_is_empty(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)

    assert store.read_activity("ghost") == []
    assert store.read_decisions("ghost") == []
    assert store.load_state("ghost") == {}


def test_a_decision_is_stored_whole(tmp_path: Path) -> None:
    store = AgentStore(tmp_path)
    record = {
        "decision_id": "dec_1",
        "action": "buy",
        "symbol": "INFY",
        "quantity": 120,
        "target": 1480.0,
        "stop_loss": 1455.0,
        "reason": "reclaim of VWAP",
    }

    store.append_decision("alpha", record)
    back = store.read_decisions("alpha")

    assert back == [{**record, "ts": back[0]["ts"]}]
    assert back[0]["quantity"] == 120

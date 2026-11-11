"""Token usage: the numbers a bill is made of, read from the activity log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from module2.definition import AgentDefinition
from module2.store import AgentStore
from module2.usage import summarise, usage_report

pytestmark = pytest.mark.unit

DAY = "2026-10-22"


def definition(agent_id: str = "alpha", **overrides: Any) -> AgentDefinition:
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "name": f"Agent {agent_id}",
        "system_prompt": "Decide on closed candles only.",
        "model": "gpt-6-luna",
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def store_in(tmp_path: Path) -> AgentStore:
    return AgentStore(tmp_path / "agents")


def activity(store: AgentStore, agent_id: str, events: list[dict[str, Any]]) -> None:
    path = store.activity_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


def reply(ts: str, **usage: Any) -> dict[str, Any]:
    return {"type": "model_reply", "ts": ts, "attempt": 1, "tool_calls": 1, **usage}


def test_the_totals_are_the_sum_of_the_reply_usage_blocks(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.save(definition())
    activity(
        store,
        "alpha",
        [
            {"type": "cycle_started", "ts": f"{DAY}T04:05:00.000Z"},
            {"type": "agent_started", "ts": f"{DAY}T04:00:00.000Z", "model": "gpt-6-luna"},
            reply(
                f"{DAY}T04:05:01.000Z",
                prompt_tokens=100,
                completion_tokens=20,
                prompt_cache_hit_tokens=80,
                prompt_cache_miss_tokens=20,
                reasoning_tokens=10,
            ),
            reply(
                f"{DAY}T04:10:01.000Z",
                prompt_tokens=50,
                completion_tokens=5,
                prompt_cache_hit_tokens=40,
                prompt_cache_miss_tokens=10,
            ),
            {"type": "tool_call", "ts": f"{DAY}T04:05:01.100Z", "tool": "screen"},
            {"type": "decision", "ts": f"{DAY}T04:05:02.000Z"},
        ],
    )

    report = usage_report(store, day=DAY)
    agent = report["agents"][0]

    assert agent["agent_id"] == "alpha"
    assert agent["model"] == "gpt-6-luna"
    assert agent["model_calls"] == 2
    assert agent["cycles"] == 1
    assert agent["tool_calls"] == 1
    assert agent["decisions"] == 1
    assert agent["prompt_tokens"] == 150
    assert agent["completion_tokens"] == 25
    assert agent["prompt_cache_hit_tokens"] == 120
    assert agent["prompt_cache_miss_tokens"] == 30
    assert agent["reasoning_tokens"] == 10
    assert agent["total_tokens"] == 175
    assert report["totals"]["prompt_tokens"] == 150
    assert report["totals"]["model_calls"] == 2


def test_the_day_is_an_ist_day_not_a_utc_one(tmp_path: Path) -> None:
    """00:30 IST is still the previous UTC day; the bill follows IST."""

    store = store_in(tmp_path)
    store.save(definition())
    activity(
        store,
        "alpha",
        [
            # 22:50 IST on the 22nd (17:20 UTC)
            reply(f"{DAY}T17:20:00.000Z", prompt_tokens=10, completion_tokens=1),
            # 00:30 IST on the 23rd (19:00 UTC on the 22nd)
            reply(f"{DAY}T19:00:00.000Z", prompt_tokens=90, completion_tokens=9),
        ],
    )

    on_22 = usage_report(store, day=DAY)
    on_23 = usage_report(store, day="2026-10-23")

    assert on_22["agents"][0]["prompt_tokens"] == 10
    assert on_23["agents"][0]["prompt_tokens"] == 90


def test_an_agent_with_no_log_is_listed_with_zeroes(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.save(definition())

    report = usage_report(store, day=DAY)

    agent = report["agents"][0]
    assert agent["model_calls"] == 0
    assert agent["prompt_tokens"] == 0
    assert agent["total_tokens"] == 0
    assert agent["models"] == ["gpt-6-luna"]
    assert report["totals"]["model_calls"] == 0


def test_a_retired_agent_is_not_in_the_usage_table(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.save(definition())
    activity(
        store,
        "alpha",
        [reply(f"{DAY}T04:05:00.000Z", prompt_tokens=10, completion_tokens=1)],
    )

    store.retire("alpha")

    assert usage_report(store, day=DAY)["agents"] == []


def test_a_day_that_is_not_a_date_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        usage_report(store_in(tmp_path), day="22-10-2026")


def test_a_log_from_before_usage_was_recorded_still_counts_calls() -> None:
    """The old format wrote model_text, not model_reply; calls are still readable."""

    row = summarise(
        [
            {"type": "model_text", "text": "looking"},
            {"type": "model_text", "text": "still looking"},
        ]
    )

    assert row["model_calls"] == 2
    assert row["prompt_tokens"] == 0
    assert row["total_tokens"] == 0


def test_summarise_ignores_a_value_that_is_not_a_number() -> None:
    row = summarise(
        [
            {"type": "model_reply", "prompt_tokens": "lots", "completion_tokens": 3},
            {"type": "model_reply", "prompt_tokens": 10},
        ]
    )

    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 3
    assert row["total_tokens"] == 13


def test_one_cycle_is_observable_with_its_tokens_and_measured_time() -> None:
    row = summarise(
        [
            {
                "type": "cycle_started",
                "cycle_id": "cyc_one",
                "ts": f"{DAY}T04:05:00.000Z",
                "bar_open_time": "2026-10-22T09:30:00+05:30",
                "timeframe": "15m",
                "market_open": True,
            },
            {
                **reply(
                    f"{DAY}T04:05:01.000Z",
                    prompt_tokens=100,
                    completion_tokens=20,
                    reasoning_tokens=8,
                ),
                "cycle_id": "cyc_one",
                "latency_ms": 1250.5,
            },
            {
                "type": "tool_call",
                "cycle_id": "cyc_one",
                "ts": f"{DAY}T04:05:01.100Z",
                "tool": "screen",
            },
            {
                "type": "tool_result",
                "cycle_id": "cyc_one",
                "ts": f"{DAY}T04:05:01.450Z",
                "tool": "screen",
                "ok": True,
                "ms": 340.0,
            },
            {
                "type": "decision",
                "cycle_id": "cyc_one",
                "ts": f"{DAY}T04:05:01.500Z",
            },
            {
                "type": "cycle_finished",
                "cycle_id": "cyc_one",
                "ts": f"{DAY}T04:05:02.000Z",
                "duration_ms": 2000.0,
                "rounds": 2,
                "decision_count": 1,
                "notes_kept": True,
                "status": "decided",
            },
        ]
    )

    assert row["cycle_rows"][0]["cycle_id"] == "cyc_one"
    assert row["cycle_rows"][0]["duration_ms"] == 2000.0
    assert row["cycle_rows"][0]["duration_source"] == "measured"
    assert row["cycle_rows"][0]["total_tokens"] == 120
    assert row["cycle_rows"][0]["model_time_ms"] == 1250.5
    assert row["cycle_rows"][0]["tool_time_ms"] == 340.0
    assert row["cycle_time_ms"] == 2000.0
    assert row["timed_cycles"] == 1


def test_an_old_cycle_without_a_finish_event_has_a_derived_duration() -> None:
    row = summarise(
        [
            {
                "type": "cycle_started",
                "cycle_id": "cyc_old",
                "ts": f"{DAY}T04:05:00.000Z",
            },
            {
                **reply(f"{DAY}T04:05:01.500Z", prompt_tokens=10),
                "cycle_id": "cyc_old",
                "latency_ms": 1500.0,
            },
        ]
    )

    assert row["cycle_rows"][0]["duration_source"] == "derived_from_log"
    assert row["cycle_rows"][0]["duration_ms"] == 1500.0

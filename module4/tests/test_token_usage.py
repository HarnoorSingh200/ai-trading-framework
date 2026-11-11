"""The token record: mirrored from Module 2 and kept once the day is over."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from module4.timeutil import IST
from module4.usage import TokenUsage, UsageStore

pytestmark = pytest.mark.unit

DAY = "2026-10-22"


class FakeUsage:
    """Module 2's answer, without a process to ask."""

    def __init__(self, reports: dict[str, dict[str, Any]]) -> None:
        self.reports = dict(reports)
        self.calls: list[str] = []

    def usage(self, day: str) -> dict[str, Any]:
        self.calls.append(day)
        return self.reports.get(
            day, {"day": day, "agents": [], "totals": {}}
        )


class BrokenUsage:
    def usage(self, day: str) -> dict[str, Any]:
        raise RuntimeError("Module 2 is resting")


def clock() -> datetime:
    return datetime(2026, 10, 22, 14, 35, tzinfo=IST)


def report(
    day: str = DAY,
    *,
    calls: int = 360,
    prompt: int = 120_000,
    hit: int = 90_000,
    miss: int = 30_000,
    completion: int = 4_000,
    reasoning: int = 1_500,
    cycles: int = 60,
    tool_calls: int = 700,
    decisions: int = 12,
) -> dict[str, Any]:
    agent = {
        "agent_id": "asd",
        "name": "asd",
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
        "model_calls": calls,
        "cycles": cycles,
        "tool_calls": tool_calls,
        "decisions": decisions,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "reasoning_tokens": reasoning,
        "total_tokens": prompt + completion,
        "models": ["gpt-6-luna"],
    }
    totals = {
        key: value
        for key, value in agent.items()
        if key not in {"agent_id", "name", "model", "reasoning_effort"}
    }
    return {"day": day, "agents": [agent], "totals": totals}


def test_a_sync_stores_the_days_totals_and_they_outlive_the_process(tmp_path: Path) -> None:
    store = UsageStore(tmp_path)
    client = FakeUsage({DAY: report()})
    usage = TokenUsage(store=store, client=client, clock=clock)

    result = usage.sync(day=DAY)

    assert result["days"] == 1
    found = usage.report(day=DAY)
    assert found["count"] == 1
    day = found["days"][0]
    assert day["day"] == DAY
    assert day["totals"]["prompt_tokens"] == 120_000
    assert day["totals"]["reasoning_tokens"] == 1_500
    assert day["agents"][0]["agent_id"] == "asd"

    # A restart reads the record back; Module 2 is not needed to remember it.
    again = TokenUsage(store=UsageStore(tmp_path), client=None, clock=clock)
    assert again.report(day=DAY)["days"][0]["totals"]["completion_tokens"] == 4_000


def test_a_later_smaller_observation_does_not_lose_tokens(tmp_path: Path) -> None:
    """A restart that reports less must not walk the day's totals backwards."""

    store = UsageStore(tmp_path)
    first = TokenUsage(
        store=store,
        client=FakeUsage({DAY: report(calls=100, prompt=1_000, completion=600)}),
        clock=clock,
    )
    first.sync(day=DAY)

    second = TokenUsage(
        store=UsageStore(tmp_path),
        client=FakeUsage({DAY: report(calls=80, prompt=800, completion=500)}),
        clock=clock,
    )
    second.sync(day=DAY)

    day = second.report(day=DAY)["days"][0]
    assert day["totals"]["model_calls"] == 100
    assert day["totals"]["prompt_tokens"] == 1_000
    assert day["totals"]["completion_tokens"] == 600


def test_a_failed_read_keeps_what_was_already_known(tmp_path: Path) -> None:
    store = UsageStore(tmp_path)
    good = TokenUsage(store=store, client=FakeUsage({DAY: report()}), clock=clock)
    good.sync(day=DAY)

    broken = TokenUsage(store=UsageStore(tmp_path), client=BrokenUsage(), clock=clock)
    result = broken.sync(day=DAY)

    assert result["days"] == 0
    assert "resting" in (result["error"] or "")
    assert broken.report(day=DAY)["days"][0]["totals"]["prompt_tokens"] == 120_000
    assert broken.status()["failures"] == 1


def test_a_plain_sync_reads_yesterday_and_today(tmp_path: Path) -> None:
    client = FakeUsage(
        {
            "2026-10-21": report("2026-10-21", calls=10, prompt=100),
            DAY: report(),
        }
    )
    usage = TokenUsage(store=UsageStore(tmp_path), client=client, clock=clock)

    usage.sync()

    assert client.calls == ["2026-10-21", "2026-10-22"]
    assert usage.report()["count"] == 2


def test_a_day_with_no_model_calls_is_not_a_usage_day(tmp_path: Path) -> None:
    zero = report(
        calls=0,
        prompt=0,
        hit=0,
        miss=0,
        completion=0,
        reasoning=0,
        cycles=0,
        tool_calls=0,
        decisions=0,
    )
    usage = TokenUsage(
        store=UsageStore(tmp_path), client=FakeUsage({DAY: zero}), clock=clock
    )

    usage.sync(day=DAY)

    assert usage.report(day=DAY)["count"] == 0


def test_an_agent_filter_narrows_totals_to_that_agent(tmp_path: Path) -> None:
    usage = TokenUsage(
        store=UsageStore(tmp_path), client=FakeUsage({DAY: report()}), clock=clock
    )
    usage.sync(day=DAY)

    found = usage.report(day=DAY, agent_id="asd")
    missing = usage.report(day=DAY, agent_id="nobody")

    assert found["days"][0]["totals"]["model_calls"] == 360
    assert found["days"][0]["agents"][0]["agent_id"] == "asd"
    assert missing["count"] == 0


def test_cycle_timing_and_tokens_survive_the_mirror(tmp_path: Path) -> None:
    payload = report()
    payload["agents"][0]["cycle_rows"] = [
        {
            "cycle_id": "cyc_one",
            "bar_open_time": "2026-10-22T09:30:00+05:30",
            "started_at": "2026-10-22T04:00:00.000Z",
            "finished_at": "2026-10-22T04:00:02.100Z",
            "duration_ms": 2100.0,
            "duration_source": "measured",
            "model_calls": 2,
            "model_time_ms": 1600.0,
            "tool_calls": 3,
            "tool_time_ms": 300.0,
            "rounds": 2,
            "decisions": 1,
            "notes_kept": True,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "reasoning_tokens": 8,
            "total_tokens": 120,
        }
    ]
    payload["agents"][0].update(
        {
            "cycle_time_ms": 2100.0,
            "model_time_ms": 1600.0,
            "tool_time_ms": 300.0,
            "timed_cycles": 1,
        }
    )
    payload["totals"].update(
        {
            "cycle_time_ms": 2100.0,
            "model_time_ms": 1600.0,
            "tool_time_ms": 300.0,
            "timed_cycles": 1,
        }
    )
    usage = TokenUsage(
        store=UsageStore(tmp_path), client=FakeUsage({DAY: payload}), clock=clock
    )

    usage.sync(day=DAY)

    day = usage.report(day=DAY)["days"][0]
    assert day["totals"]["cycle_time_ms"] == 2100.0
    assert day["agents"][0]["cycle_rows"][0]["cycle_id"] == "cyc_one"
    assert day["agents"][0]["cycle_rows"][0]["total_tokens"] == 120

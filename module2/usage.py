"""Token usage, summed from the activity each agent already writes.

Module 2 owns the agent logs, so this is where the question "what did the
models cost" is answered. The aggregation lives here and the HTTP surface only
serves it: the numbers are counts over the `model_reply` events the worker
already records (one per model call, with the provider's usage block), not a
second accounting system that could disagree with the log.

Only fields the provider actually reported are summed. A provider that does not
say how many of its completion tokens were reasoning contributes no reasoning
tokens - a guess would be worse than a zero.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from module2.store import AgentStore
from module2.timeutil import IST, format_utc, ist_today, parse_timestamp, utc_now

#: The token fields a model reply may carry. They are the provider's own
#: vocabulary, flattened by the model client; a missing field is "not reported".
TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)

#: What the activity log can count per day besides tokens.
COUNT_FIELDS: Final[tuple[str, ...]] = (
    "model_calls",
    "cycles",
    "tool_calls",
    "decisions",
)

#: Timing totals that can be summed across agents and days. ``timed_cycles``
#: says how many durations are real observations, so an average never divides
#: by a cycle count that had no timing event.
TIMING_FIELDS: Final[tuple[str, ...]] = (
    "cycle_time_ms",
    "model_time_ms",
    "tool_time_ms",
    "timed_cycles",
)


def events_for_day(path: Path, day: str) -> Iterator[dict[str, Any]]:
    """Every activity event whose timestamp falls on `day` in IST.

    The log is append-only and in time order, so a torn final line (the log is
    always caught mid-write by someone) is skipped rather than raised, exactly
    as the store's own tail reader does.
    """

    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                found = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(found, dict):
                continue
            if _day_of(found.get("ts")) == day:
                yield found


def _day_of(value: Any) -> str | None:
    """The IST date of an event timestamp, or None when it cannot be read."""

    if not isinstance(value, str):
        return None
    try:
        moment = parse_timestamp(value)
    except ValueError:
        return None
    return moment.astimezone(IST).date().isoformat()


def _number(value: Any) -> float:
    """A finite numeric observation, or zero when the field is absent."""

    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _integer(value: Any) -> int:
    """A non-negative count from a log field."""

    return max(0, int(_number(value)))


def _timestamp_ms(value: Any) -> float | None:
    """A timestamp's epoch milliseconds, or ``None`` when it cannot be read."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment: datetime = parse_timestamp(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() * 1000.0


def summarise(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """One agent's totals, plus one observation per cycle.

    The aggregate answers what the day cost. ``cycle_rows`` answers where that
    cost went. A cycle is grouped by the kernel-assigned ``cycle_id`` the worker
    already writes on every model reply and tool result; the explicit
    ``cycle_finished`` event supplies a process-measured wall duration. Older
    logs without that event are still readable, but their duration is labelled
    as derived from the first and last timestamp rather than presented as a
    direct measurement.
    """

    found = [dict(event) for event in events]
    counts = dict.fromkeys(COUNT_FIELDS, 0)
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    models: set[str] = set()
    replies = 0
    texts = 0
    cycles: dict[str, dict[str, Any]] = {}

    def cycle_for(cycle_id: Any) -> dict[str, Any] | None:
        key = str(cycle_id or "").strip()
        if not key:
            return None
        return cycles.setdefault(
            key,
            {
                "cycle_id": key,
                "bar_open_time": None,
                "timeframe": None,
                "market_open": None,
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "duration_source": None,
                "model_calls": 0,
                "model_time_ms": 0.0,
                "tool_calls": 0,
                "tool_time_ms": 0.0,
                "rounds": None,
                "decisions": 0,
                "notes_kept": None,
                "status": None,
                "total_tokens": 0,
                **dict.fromkeys(TOKEN_FIELDS, 0),
                "_first_ms": None,
                "_last_ms": None,
            },
        )

    for event in found:
        kind = event.get("type")
        cycle = cycle_for(event.get("cycle_id"))
        if cycle is not None:
            moment = _timestamp_ms(event.get("ts"))
            if moment is not None:
                first = cycle["_first_ms"]
                cycle["_first_ms"] = moment if first is None else min(first, moment)
                last = cycle["_last_ms"]
                cycle["_last_ms"] = moment if last is None else max(last, moment)
        if kind == "model_reply":
            replies += 1
            for field in TOKEN_FIELDS:
                value = event.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens[field] += value
                    if cycle is not None:
                        cycle[field] += value
            if cycle is not None:
                cycle["model_calls"] += 1
                cycle["model_time_ms"] += _number(event.get("latency_ms"))
                cycle["total_tokens"] = (
                    cycle["prompt_tokens"] + cycle["completion_tokens"]
                )
        elif kind == "cycle_started":
            counts["cycles"] += 1
            if cycle is not None:
                cycle["bar_open_time"] = event.get("bar_open_time")
                cycle["timeframe"] = event.get("timeframe")
                cycle["market_open"] = event.get("market_open")
                cycle["started_at"] = event.get("ts")
        elif kind == "cycle_finished":
            if cycle is not None:
                cycle["finished_at"] = event.get("ts")
                cycle["duration_ms"] = _number(event.get("duration_ms"))
                cycle["duration_source"] = "measured"
                cycle["rounds"] = _integer(event.get("rounds"))
                cycle["decisions"] = _integer(event.get("decision_count"))
                cycle["notes_kept"] = bool(event.get("notes_kept"))
                cycle["status"] = event.get("status")
        elif kind == "tool_call":
            counts["tool_calls"] += 1
            if cycle is not None:
                cycle["tool_calls"] += 1
        elif kind == "tool_result":
            if cycle is not None:
                cycle["tool_time_ms"] += _number(event.get("ms"))
        elif kind == "decision":
            counts["decisions"] += 1
            if cycle is not None:
                cycle["decisions"] += 1
        elif kind == "model_text":
            # The activity log before token usage was recorded wrote one
            # model_text per reply instead of a model_reply event. Counting
            # those keeps an older session's call count honest; its token
            # counts stay zero because the provider's usage was never stored.
            texts += 1
        elif kind == "agent_started":
            model = event.get("model")
            if isinstance(model, str) and model.strip():
                models.add(model.strip())

    # A cycle event may be present without a readable cycle_started after a
    # torn write. The unique ids are the factual count; the old explicit count
    # is retained as a floor for logs predating cycle ids.
    counts["cycles"] = max(counts["cycles"], len(cycles))
    counts["model_calls"] = replies if replies else texts
    row: dict[str, Any] = {**counts, **tokens}
    row["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]
    row["models"] = sorted(models)

    cycle_rows: list[dict[str, Any]] = []
    for cycle in cycles.values():
        duration = cycle["duration_ms"]
        if (
            duration is None
            and cycle["_first_ms"] is not None
            and cycle["_last_ms"] is not None
        ):
            duration = max(0.0, cycle["_last_ms"] - cycle["_first_ms"])
            cycle["duration_source"] = "derived_from_log"
        cycle["duration_ms"] = (
            None if duration is None else round(float(duration), 1)
        )
        cycle["model_time_ms"] = round(float(cycle["model_time_ms"]), 1)
        cycle["tool_time_ms"] = round(float(cycle["tool_time_ms"]), 1)
        cycle["rounds"] = _integer(cycle["rounds"])
        cycle["decisions"] = _integer(cycle["decisions"])
        cycle.pop("_first_ms", None)
        cycle.pop("_last_ms", None)
        cycle_rows.append(cycle)
    cycle_rows.sort(
        key=lambda row: (
            str(row.get("started_at") or ""),
            str(row.get("cycle_id") or ""),
        )
    )
    timed = [row for row in cycle_rows if row.get("duration_ms") is not None]
    row["cycle_rows"] = cycle_rows
    row["cycle_time_ms"] = round(
        sum(float(item["duration_ms"]) for item in timed), 1
    )
    row["model_time_ms"] = round(
        sum(float(item.get("model_time_ms") or 0.0) for item in cycle_rows), 1
    )
    row["tool_time_ms"] = round(
        sum(float(item.get("tool_time_ms") or 0.0) for item in cycle_rows), 1
    )
    row["timed_cycles"] = len(timed)
    return row


def usage_report(store: AgentStore, *, day: str | None = None) -> dict[str, Any]:
    """Every defined agent's token use on one exchange day, and the total.

    A defined agent with no activity that day is listed with zeros - the
    dashboard should not have to ask which agents exist separately from what
    they spent. Retired agents are not listed; their directory is kept, but
    they are no longer part of the fleet.
    """

    chosen = _valid_day(day) if day else ist_today().isoformat()
    agents: list[dict[str, Any]] = []
    totals: dict[str, Any] = {
        **dict.fromkeys(COUNT_FIELDS, 0),
        **dict.fromkeys(TOKEN_FIELDS, 0),
        **dict.fromkeys(TIMING_FIELDS, 0),
        "total_tokens": 0,
        "models": [],
    }
    models: set[str] = set()
    for definition in store.list_definitions():
        row = summarise(events_for_day(store.activity_path(definition.agent_id), chosen))
        if not row["models"]:
            row["models"] = [definition.model] if definition.model else []
        row.update(
            {
                "agent_id": definition.agent_id,
                "name": definition.name,
                "model": definition.model,
                "reasoning_effort": definition.reasoning_effort,
            }
        )
        agents.append(row)
        for field in (*COUNT_FIELDS, *TOKEN_FIELDS, *TIMING_FIELDS, "total_tokens"):
            totals[field] += row[field]
        models.update(row["models"])
    totals["models"] = sorted(models)
    return {
        "day": chosen,
        "generated_at": format_utc(utc_now()),
        "agents": agents,
        "totals": totals,
    }


def _valid_day(day: str) -> str:
    try:
        return date.fromisoformat(str(day)).isoformat()
    except ValueError as exc:
        raise ValueError(f"day must be YYYY-MM-DD, not {day!r}") from exc

"""AI token usage and cycle timing, read from Module 2 and kept beside the archive.

Module 2 owns the activity logs, and its own route already sums a day's model
replies into tokens and pairs `cycle_started`/`cycle_finished` events into
per-cycle observations. This module asks that route for the day and keeps what
it is told, so the operator can still see what the agents cost and how long a
cycle took after the agent engine - and its logs - have gone to rest.

Module 2's activity log is not read from here. A module that reads another
module's files has one interface for reading it and none for keeping it, and
the first refactor of that storage breaks a module nobody told.

The numbers are monotonic inside a day: a call that happened cannot un-happen,
so merging keeps the larger of two observations. A read that fails leaves what
was already known untouched; a stale total is better than a missing one.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from module4.store import read_json, write_json
from module4.timeutil import IST, format_utc, utc_now

#: One local record. It carries daily token totals and the per-cycle rows that
#: explain them. It is intentionally separate from the S3-mirrored order
#: archive because it is agent telemetry, not a trading record.
USAGE_FILE: Final[str] = "tokens.json"

#: The token fields Module 2 reports. They are the provider's vocabulary,
#: flattened by Module 2's model client.
TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)

#: What a day can count besides tokens.
COUNT_FIELDS: Final[tuple[str, ...]] = (
    "model_calls",
    "cycles",
    "tool_calls",
    "decisions",
)

TIMING_FIELDS: Final[tuple[str, ...]] = (
    "cycle_time_ms",
    "model_time_ms",
    "tool_time_ms",
    "timed_cycles",
)

_NUMERIC_FIELDS: Final[tuple[str, ...]] = (
    *COUNT_FIELDS,
    *TOKEN_FIELDS,
    *TIMING_FIELDS,
    "total_tokens",
)

_CYCLE_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "cycle_id",
    "bar_open_time",
    "timeframe",
    "market_open",
    "started_at",
    "finished_at",
    "duration_ms",
    "duration_source",
    "model_calls",
    "model_time_ms",
    "tool_calls",
    "tool_time_ms",
    "rounds",
    "decisions",
    "notes_kept",
    "status",
    *TOKEN_FIELDS,
    "total_tokens",
)
_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "agent_id",
    "name",
    "model",
    "reasoning_effort",
)


class UsageStore:
    """Reads and writes the token-usage record under one directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def path(self) -> Path:
        return self._root / USAGE_FILE

    def load(self) -> dict[str, dict[str, Any]]:
        payload = read_json(self.path)
        if not isinstance(payload, Mapping):
            return {}
        days = payload.get("days")
        if not isinstance(days, Mapping):
            return {}
        return {
            str(day): dict(value)
            for day, value in days.items()
            if isinstance(value, Mapping)
        }

    def save(self, days: Mapping[str, Mapping[str, Any]]) -> None:
        write_json(
            self.path,
            {
                "written_at": format_utc(utc_now()),
                "days": {str(day): dict(value) for day, value in days.items()},
            },
        )


class TokenUsage:
    """The token record, and the sync that keeps it current."""

    def __init__(
        self,
        *,
        store: UsageStore,
        client: Any = None,
        clock: Any = utc_now,
    ) -> None:
        self._store = store
        self._client = client
        self._clock = clock
        self._lock = threading.RLock()
        self._days: dict[str, dict[str, Any]] = store.load()
        self.synced_at: Any = None
        self.syncs = 0
        self.failures = 0
        self.last_error: str | None = None

    # -- bringing the record up to date -------------------------------------- #

    def sync(self, *, day: str | None = None) -> dict[str, Any]:
        """Read Module 2's totals for one day, or for today and yesterday.

        Both days are read because the archive is written in IST while a log
        is appended across midnight: a sync at 00:01 must still capture the
        day that just ended, and one at 23:59 must not lose the day still
        being written.
        """

        client = self._client
        if client is None:
            return {"skipped": "no agent engine is configured"}
        wanted = self._wanted(day)
        fetched = 0
        errors: list[str] = []
        for wanted_day in wanted:
            try:
                report = client.usage(wanted_day)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
                continue
            if not isinstance(report, Mapping):
                errors.append("the usage report was not an object")
                continue
            self._merge(report)
            fetched += 1
        if fetched:
            self._store.save(self._days)
            self.synced_at = self._clock()
            self.syncs += 1
        self.failures += len(errors)
        self.last_error = errors[-1] if errors else None
        return {"days": fetched, "wanted": wanted, "error": self.last_error}

    def _wanted(self, day: str | None) -> list[str]:
        if day is not None:
            return [str(day)]
        today = self._clock().astimezone(IST).date()
        return [
            (today - timedelta(days=1)).isoformat(),
            today.isoformat(),
        ]

    def _merge(self, report: Mapping[str, Any]) -> None:
        found_day = str(report.get("day") or "").strip()
        if not found_day:
            return
        rows = report.get("agents")
        with self._lock:
            held = self._days.get(found_day)
            if not isinstance(held, Mapping):
                held = {"day": found_day, "agents": {}, "totals": {}}
            agents = dict(held.get("agents") or {})
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    agent_id = str(row.get("agent_id") or "").strip()
                    if not agent_id:
                        continue
                    agents[agent_id] = _merge_row(agents.get(agent_id), row)
            totals = _merge_row(
                held.get("totals") if isinstance(held.get("totals"), Mapping) else None,
                report.get("totals")
                if isinstance(report.get("totals"), Mapping)
                else {},
            )
            self._days[found_day] = {
                "day": found_day,
                "agents": agents,
                "totals": totals,
            }

    # -- reading it back ------------------------------------------------------ #

    def report(
        self, *, day: str | None = None, agent_id: str | None = None
    ) -> dict[str, Any]:
        """The stored usage, newest day first, optionally narrowed to one day."""

        with self._lock:
            held = {
                str(key): {
                    "agents": dict(value.get("agents") or {}),
                    "totals": dict(value.get("totals") or {}),
                }
                for key, value in self._days.items()
                if isinstance(value, Mapping)
            }
        rows: list[dict[str, Any]] = []
        for found_day in sorted(held, reverse=True):
            if day is not None and found_day != day:
                continue
            agents = held[found_day]["agents"]
            if agent_id is not None:
                selected = (
                    [dict(agents[agent_id])] if agent_id in agents else []
                )
                totals = _totals_of(selected)
            else:
                selected = [dict(agents[key]) for key in sorted(agents)]
                totals = dict(held[found_day]["totals"])
            if not _has_usage(totals):
                # A day that was read before a model call is not a usage day
                # yet; an empty row on the dashboard says nothing.
                continue
            rows.append({"day": found_day, "agents": selected, "totals": totals})
        return {
            "count": len(rows),
            "days": rows,
            "tracked_days": len(held),
            "updated_at": None if self.synced_at is None else format_utc(self.synced_at),
        }

    def status(self) -> dict[str, Any]:
        return {
            "days": len(self._days),
            "updated_at": None if self.synced_at is None else format_utc(self.synced_at),
            "syncs": self.syncs,
            "failures": self.failures,
            "last_error": self.last_error,
            "file": str(self._store.path),
        }


def _merge_row(
    held: Mapping[str, Any] | None, incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """One observation folded into another, keeping every token that was seen."""

    merged = dict(held or {})
    for field, value in incoming.items():
        if field in _NUMERIC_FIELDS:
            number = _numeric(field, value)
            if number is not None:
                current = merged.get(field)
                merged[field] = (
                    number
                    if not isinstance(current, (int, float))
                    or isinstance(current, bool)
                    else max(current, number)
                )
        elif field == "models":
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                merged["models"] = sorted(
                    {str(item) for item in value if str(item).strip()}
                )
        elif field == "cycle_rows" and isinstance(value, Sequence) and not isinstance(
            value, (str, bytes)
        ):
            merged["cycle_rows"] = _merge_cycles(merged.get("cycle_rows"), value)
        elif field in _IDENTITY_FIELDS and value not in (None, ""):
            merged[field] = value
    return merged


def _merge_cycles(
    held: Any, incoming: Sequence[Any]
) -> list[dict[str, Any]]:
    """Keep one settled row per cycle id, newest observation winning.

    A cycle's work is finished before the next one starts. The same id should
    therefore describe the same work on every sync; replacing the row prevents
    a second partial read from leaving stale token or timing numbers beside a
    newer status. Rows without an id are ignored rather than assigned a guess.
    """

    merged: dict[str, dict[str, Any]] = {}
    if isinstance(held, Sequence) and not isinstance(held, (str, bytes)):
        for row in held:
            if isinstance(row, Mapping) and str(row.get("cycle_id") or "").strip():
                merged[str(row["cycle_id"])] = dict(row)
    for row in incoming:
        if not isinstance(row, Mapping):
            continue
        cycle_id = str(row.get("cycle_id") or "").strip()
        if not cycle_id:
            continue
        merged[cycle_id] = {
            **merged.get(cycle_id, {}),
            **{
                key: value
                for key, value in row.items()
                if key in _CYCLE_IDENTITY_FIELDS and value is not None
            },
        }
    return sorted(
        merged.values(),
        key=lambda row: (
            str(row.get("started_at") or ""),
            str(row.get("cycle_id") or ""),
        ),
    )


def _numeric(field: str, value: Any) -> int | float | None:
    """Keep a whole count whole and a millisecond measurement fractional."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    if field in (*COUNT_FIELDS, *TOKEN_FIELDS, "timed_cycles", "total_tokens"):
        return int(number)
    return round(number, 1)


def _totals_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {**dict.fromkeys(_NUMERIC_FIELDS, 0), "models": []}
    models: set[str] = set()
    for row in rows:
        for field in _NUMERIC_FIELDS:
            value = row.get(field)
            number = _numeric(field, value)
            if number is not None:
                totals[field] += number
        found = row.get("models")
        if isinstance(found, Sequence) and not isinstance(found, (str, bytes)):
            models.update(str(item) for item in found if str(item).strip())
    totals["models"] = sorted(models)
    return totals


def _has_usage(totals: Mapping[str, Any]) -> bool:
    return any(
        isinstance(totals.get(field), int) and totals[field] > 0
        for field in _NUMERIC_FIELDS
    )

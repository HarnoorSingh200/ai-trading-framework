"""The registry, and the two logs each agent keeps.

Everything an agent is and everything it did lives in one directory per agent:

* ``definition.json`` - what it is (rewritten atomically when it is edited);
* ``activity.jsonl`` - what it was doing, one JSON object per line, appended;
* ``decisions.jsonl`` - what it decided, appended, never rewritten;
* ``state.json`` - where its loop got to, so a restart does not repeat a bar;
* ``notes.json`` - the notebook it carries forward, rewritten atomically when
  it writes a note (see :mod:`module2.notes` for its shape and its bounds).

Two logs rather than a database, because the point of both of them is to be
readable while they are being written: the operator site tails them, and a
human debugging an agent wants to see the last twenty lines rather than write a
query. A torn final line is skipped rather than raised - a log is always caught
mid-write by someone.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from module2.definition import AgentDefinition
from module2.timeutil import format_utc, utc_now

DEFINITION_FILE: Final[str] = "definition.json"
ACTIVITY_FILE: Final[str] = "activity.jsonl"
DECISIONS_FILE: Final[str] = "decisions.jsonl"
STATE_FILE: Final[str] = "state.json"
NOTES_FILE: Final[str] = "notes.json"

#: Where a retired agent's directory is moved, rather than deleted: deleting an
#: agent must not delete what it decided.
RETIRED_DIR: Final[str] = "_retired"


class AgentStore:
    """Reads and writes the agent registry under one directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def agent_dir(self, agent_id: str) -> Path:
        return self._root / agent_id

    def definition_path(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / DEFINITION_FILE

    def activity_path(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / ACTIVITY_FILE

    def decisions_path(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / DECISIONS_FILE

    def state_path(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / STATE_FILE

    def notes_path(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / NOTES_FILE

    # -- the registry -------------------------------------------------------- #

    def exists(self, agent_id: str) -> bool:
        return self.definition_path(agent_id).exists()

    def save(self, definition: AgentDefinition) -> AgentDefinition:
        """Write a definition atomically: a reader sees the old one or the new."""

        _write_json(self.definition_path(definition.agent_id), definition.describe())
        return definition

    def load(self, agent_id: str) -> AgentDefinition | None:
        payload = _read_json(self.definition_path(agent_id))
        if payload is None:
            return None
        return AgentDefinition.from_dict(payload)

    def list_definitions(self) -> list[AgentDefinition]:
        """Every defined agent, by id. Retired ones are not listed."""

        if not self._root.exists():
            return []
        found: list[AgentDefinition] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            definition = self.load(entry.name)
            if definition is not None:
                found.append(definition)
        return found

    def retire(self, agent_id: str) -> Path | None:
        """Stop listing an agent while keeping everything it did.

        The directory is moved rather than removed, because the decisions in it
        are the only record of what that agent concluded.
        """

        source = self.agent_dir(agent_id)
        if not source.exists():
            return None
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        target = self._root / RETIRED_DIR / f"{agent_id}-{stamp}"
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        return target

    # -- the logs ------------------------------------------------------------ #

    def append_activity(self, agent_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append one line of activity, stamped with when it happened."""

        stored = {**event, "ts": format_utc(event.get("at") or utc_now())}
        stored.pop("at", None)
        _append(self.activity_path(agent_id), stored)
        return stored

    def read_activity(self, agent_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return _read_tail(self.activity_path(agent_id), limit=limit)

    def append_decision(self, agent_id: str, record: dict[str, Any]) -> dict[str, Any]:
        stored = {**record, "ts": format_utc(utc_now())}
        _append(self.decisions_path(agent_id), stored)
        return stored

    def read_decisions(self, agent_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return _read_tail(self.decisions_path(agent_id), limit=limit)

    # -- the loop's own memory ----------------------------------------------- #

    def load_state(self, agent_id: str) -> dict[str, Any]:
        """Where the loop got to. An agent that never ran has no state."""

        return _read_json(self.state_path(agent_id)) or {}

    def save_state(self, agent_id: str, state: dict[str, Any]) -> None:
        _write_json(self.state_path(agent_id), {**state, "updated_at": format_utc(utc_now())})

    # -- the notebook -------------------------------------------------------- #

    def load_notes(self, agent_id: str) -> dict[str, Any] | None:
        """The notebook as it was last written, or ``None`` if it never was."""

        return _read_json(self.notes_path(agent_id))

    def save_notes(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Rewrite the notebook atomically, like every other small file here."""

        _write_json(self.notes_path(agent_id), payload)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a small JSON file atomically, so a reader never sees half of one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _append(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")
        handle.flush()


def _read_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    """The last ``limit`` whole lines. A torn final line is skipped."""

    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        body = line.strip()
        if not body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            # A line being written right now, or one that was interrupted.
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records[-max(1, int(limit)) :]


def event_at(event: dict[str, Any]) -> datetime | None:
    """The moment an activity event carries, for anything that needs to sort."""

    from module2.timeutil import parse_timestamp

    try:
        return parse_timestamp(str(event.get("ts")))
    except (TypeError, ValueError):
        return None

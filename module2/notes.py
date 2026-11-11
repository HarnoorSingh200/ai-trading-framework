"""The agent's own notebook: the reasoning it carries forward forever.

Every other kind of memory in this framework is measured. The candles come from
Module 1, the rupees and the R-multiples come from Module 3's order book. This
is the one place an agent may write down what it *concluded* - the pattern it
noticed, the mistake it made, the rule it wants to keep - and it survives every
cycle, every restart and every night, because a lesson that dies at midnight is
not a lesson.

It is deliberately small, keyed and bounded:

* **keyed** - one lesson per key, so the model edits one thought instead of
  rewriting the whole notebook (an edit costs the tokens of the edit, not of
  the notebook);
* **bounded** - so it can be re-read at the start of every cycle without
  costing more than the notebook is worth. A note that no longer earns its
  place is deleted, not accumulated.
* **advisory** - notes are read by the model that wrote them and by the
  operator. Nothing in the framework interprets them: sizing, risk and the
  trading window are unchanged by whatever a note says.

The shape of ``data/agents/<id>/notes.json`` (version 2):

    {
      "version": 2,
      "updated_at": "...",
      "keys": {
        "vwap_reclaim": {
          "text": "reclaims held only when volume_ratio > 1.5 (4 of 5)",
          "created_at": "...", "updated_at": "...", "edits": 3
        }
      },
      "sessions": {
        "2026-10-22": {
          "saved_at": "...", "cycle_id": "cyc_...", "count": 1,
          "chars": 53,
          "keys": {"vwap_reclaim": "reclaims held only when ..."}
        }
      }
    }

``keys`` is the live notebook the model edits. ``sessions`` holds the most
recent end-of-day checkpoints, and is the hand-off the next session sees.
"""

from __future__ import annotations

import re
from typing import Any, Final

from module2.store import AgentStore
from module2.timeutil import format_utc, utc_now

NOTES_VERSION: Final[int] = 2

#: One key is one lesson. Twenty-four is more than anyone keeps honestly.
MAX_KEYS: Final[int] = 24
MAX_KEY_CHARS: Final[int] = 40
#: One lesson. Long enough for a condition, an outcome and a count; short
#: enough that the whole notebook still fits in a prompt.
MAX_NOTE_CHARS: Final[int] = 600
#: The whole notebook, all keys together.
MAX_TOTAL_CHARS: Final[int] = 6000
#: What the model is shown every cycle before it is asked anything.
MAX_DIGEST_CHARS: Final[int] = 2200
#: End-of-day checkpoints are an audit trail and a hand-off to the next
#: session. The live notebook remains the source the model edits; five saved
#: sessions are enough to show that the hand-off happened without making this
#: small file grow forever.
MAX_SESSIONS: Final[int] = 5

_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class NoteError(ValueError):
    """A notes request that cannot be honoured, said in words the model can use."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def _now() -> str:
    return format_utc(utc_now())


def _blank() -> dict[str, Any]:
    return {
        "version": NOTES_VERSION,
        "updated_at": _now(),
        "keys": {},
        "sessions": {},
    }


def load(store: AgentStore, agent_id: str) -> dict[str, dict[str, Any]]:
    """Every note this agent has, oldest key first as written."""

    document = _document(store, agent_id)
    keys = document.get("keys")
    if not isinstance(keys, dict):
        return {}
    return {str(key): dict(value) for key, value in keys.items() if isinstance(value, dict)}


def document(store: AgentStore, agent_id: str) -> dict[str, Any]:
    """What the operator sees: the whole notebook, with its key list."""

    saved = _document(store, agent_id)
    held = load(store, agent_id)
    sessions = _sessions(saved)
    latest = next(iter(sessions.items()), None)
    return {
        "version": NOTES_VERSION,
        "updated_at": saved.get("updated_at"),
        "count": len(held),
        "capacity": MAX_KEYS,
        "chars": total_chars(held),
        "char_limit": MAX_TOTAL_CHARS,
        "note_char_limit": MAX_NOTE_CHARS,
        "notes": [
            {
                "key": key,
                "text": str(value.get("text") or ""),
                "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at"),
                "edits": int(value.get("edits") or 0),
            }
            for key, value in held.items()
        ],
        "last_checkpoint": (
            None
            if latest is None
            else {
                "day": latest[0],
                **{
                    key: latest[1].get(key)
                    for key in ("saved_at", "cycle_id", "count", "chars")
                },
            }
        ),
        "checkpoints": [
            {
                "day": day,
                **{
                    key: saved_session.get(key)
                    for key in ("saved_at", "cycle_id", "count", "chars")
                },
            }
            for day, saved_session in sessions.items()
        ],
    }


def total_chars(held: dict[str, dict[str, Any]]) -> int:
    return sum(len(str(value.get("text") or "")) for value in held.values())


def apply(
    store: AgentStore,
    agent_id: str,
    *,
    action: str,
    key: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Add, extend or remove one note, and answer with what changed.

    ``write`` replaces a key (creating it if it is new), ``append`` extends the
    note already there, and ``delete`` removes it. Every refusal names the rule
    that stopped it - a model that is told "the notebook is full" can delete a
    note that is no longer true; a model that is told "no" cannot do anything.
    """

    wanted = str(action or "").strip().lower()
    if wanted not in {"write", "append", "delete"}:
        raise NoteError(
            f"{action!r} is not an action; it is write, append or delete", field="action"
        )
    held = load(store, agent_id)
    name = _checked_key(key)
    if wanted == "delete":
        if name not in held:
            raise NoteError(f"there is no note under the key {name!r}", field="key")
        removed = held.pop(name)
        _save(store, agent_id, held)
        return {
            "action": "delete",
            "key": name,
            "count": len(held),
            "removed_chars": len(str(removed.get("text") or "")),
        }

    body = " ".join(str(text or "").split())
    if not body:
        raise NoteError("a note needs text", field="text")
    if len(body) > MAX_NOTE_CHARS:
        raise NoteError(
            f"the note is {len(body)} characters; one note may hold "
            f"{MAX_NOTE_CHARS}. Split it across keys, or write the shorter lesson.",
            field="text",
        )
    existing = held.get(name)
    if wanted == "append":
        if existing is None:
            combined = body
        else:
            combined = f"{str(existing.get('text') or '').rstrip()} {body}".strip()
        if len(combined) > MAX_NOTE_CHARS:
            raise NoteError(
                f"{name!r} would reach {len(combined)} characters, past the "
                f"{MAX_NOTE_CHARS} a note may hold: rewrite it with write, or "
                "delete what is no longer true first",
                field="text",
            )
    else:
        combined = body
    if existing is None and len(held) >= MAX_KEYS:
        raise NoteError(
            f"the notebook already holds {MAX_KEYS} keys, the most it may: "
            "delete a note that is no longer true before adding another",
            field="key",
        )
    projected = total_chars(held) - len(str((existing or {}).get("text") or "")) + len(combined)
    if projected > MAX_TOTAL_CHARS:
        raise NoteError(
            f"the notebook would hold {projected} characters, past its "
            f"{MAX_TOTAL_CHARS}: delete or shorten a note first",
            field="text",
        )

    stamp = _now()
    held[name] = {
        "text": combined,
        "created_at": str((existing or {}).get("created_at") or stamp),
        "updated_at": stamp,
        "edits": int((existing or {}).get("edits") or 0) + 1,
    }
    _save(store, agent_id, held)
    return {
        "action": wanted,
        "key": name,
        "count": len(held),
        "chars": len(combined),
        "text": combined,
    }


def clear(store: AgentStore, agent_id: str) -> int:
    """Forget every note. The operator's escape hatch for a bad lesson."""

    held = load(store, agent_id)
    _save(store, agent_id, {})
    return len(held)


def digest(store: AgentStore, agent_id: str, *, day: str | None = None) -> str:
    """The notebook as the model is shown it, at the start of every cycle.

    Oldest key first, because a lesson written first is usually the one the
    agent has had the most chances to disprove - and if the whole notebook does
    not fit, the tail is named rather than silently dropped.
    """

    held = load(store, agent_id)
    previous = previous_checkpoint(store, agent_id, day=day)
    handoff = (
        ""
        if previous is None
        else (
            f" Last end-of-day checkpoint: {previous['day']} at "
            f"{previous.get('saved_at') or 'an unrecorded time'} "
            f"({previous.get('count') or 0} lessons)."
        )
    )
    if not held:
        return (
            "Your notebook is empty. It is yours to fill: once this candle's "
            "decisions are made, write down what it taught you - the first entry "
            "is the one tomorrow is read against."
        )
    lines: list[str] = []
    used = 0
    shown = 0
    for key, value in held.items():
        line = f"- {key}: {str(value.get('text') or '')}"
        if used + len(line) + 1 > MAX_DIGEST_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    hidden = len(held) - shown
    body = "\n".join(lines)
    tail = (
        f"\n({hidden} more note{'s' if hidden != 1 else ''} not shown here - "
        "call the notes tool with action='list'.)"
        if hidden
        else ""
    )
    return (
        "Your notebook (persistent across every cycle and session)."
        f"{handoff}\n{body}{tail}"
    )


def checkpoint_session(
    store: AgentStore,
    agent_id: str,
    *,
    day: str,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Save the notebook as it stood at the final cycle of an exchange day.

    The live keys are already durable between cycles. This checkpoint is the
    explicit hand-off: it proves what was known when the day ended, gives the
    next session a dated memory anchor, and keeps the most recent sessions
    without ever allowing the notebook to grow into a transcript.
    """

    from datetime import date

    try:
        saved_day = date.fromisoformat(str(day)).isoformat()
    except ValueError as exc:
        raise NoteError("a session checkpoint needs a YYYY-MM-DD day", field="day") from exc
    document_now = _document(store, agent_id)
    held = load(store, agent_id)
    sessions = _sessions(document_now)
    stamp = _now()
    sessions[saved_day] = {
        "saved_at": stamp,
        "cycle_id": cycle_id,
        "count": len(held),
        "chars": total_chars(held),
        "keys": {
            key: str(value.get("text") or "")
            for key, value in held.items()
        },
    }
    # Newest day first in the stored document; only a small fixed window is
    # retained. The live notebook remains complete within its own limits.
    ordered = dict(sorted(sessions.items(), reverse=True)[:MAX_SESSIONS])
    store.save_notes(
        agent_id,
        {
            "version": NOTES_VERSION,
            "updated_at": stamp,
            "keys": held,
            "sessions": ordered,
        },
    )
    return {
        "day": saved_day,
        "saved_at": stamp,
        "cycle_id": cycle_id,
        "count": len(held),
        "chars": total_chars(held),
    }


def previous_checkpoint(
    store: AgentStore, agent_id: str, *, day: str | None = None
) -> dict[str, Any] | None:
    """The newest checkpoint before ``day``, or the newest saved one."""

    sessions = _sessions(_document(store, agent_id))
    if not sessions:
        return None
    if day:
        earlier = [key for key in sessions if key < str(day)]
        if earlier:
            chosen = max(earlier)
            return {"day": chosen, **sessions[chosen]}
        return None
    chosen = next(iter(sessions))
    return {"day": chosen, **sessions[chosen]}


def _checked_key(key: str | None) -> str:
    name = str(key or "").strip().lower()
    if not name:
        raise NoteError("a note needs a key", field="key")
    if len(name) > MAX_KEY_CHARS:
        raise NoteError(
            f"the key is {len(name)} characters; the most is {MAX_KEY_CHARS}", field="key"
        )
    if not _KEY_PATTERN.match(name):
        raise NoteError(
            f"{name!r} is not a key: use lowercase letters, digits, dots, "
            "dashes or underscores, starting with a letter or digit",
            field="key",
        )
    return name


def _document(store: AgentStore, agent_id: str) -> dict[str, Any]:
    payload = store.load_notes(agent_id)
    return payload if isinstance(payload, dict) else _blank()


def _save(store: AgentStore, agent_id: str, held: dict[str, dict[str, Any]]) -> None:
    # A note edit during a session must not erase yesterday's end-of-day
    # checkpoints. Read the current document and carry its session history
    # forward whenever the live key set changes.
    sessions = _sessions(_document(store, agent_id))
    store.save_notes(
        agent_id,
        {
            "version": NOTES_VERSION,
            "updated_at": _now(),
            "keys": held,
            "sessions": sessions,
        },
    )


def _sessions(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = document.get("sessions")
    if not isinstance(found, dict):
        return {}
    sessions = {
        str(day): dict(value)
        for day, value in found.items()
        if isinstance(value, dict)
    }
    # The document stores newest first, but sorting here makes a hand-edited or
    # older file deterministic as well.
    return dict(sorted(sessions.items(), reverse=True)[:MAX_SESSIONS])

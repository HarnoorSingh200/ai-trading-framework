"""The notebook: what it keeps, what it refuses, and what the model sees."""

from __future__ import annotations

from pathlib import Path

import pytest

from module2 import notes
from module2.notes import NoteError
from module2.store import AgentStore

pytestmark = pytest.mark.unit


def store(tmp_path: Path) -> AgentStore:
    held = AgentStore(tmp_path / "agents")
    held.agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    return held


def test_a_written_note_is_read_back_and_survives_a_new_store(tmp_path: Path) -> None:
    held = store(tmp_path)

    notes.apply(
        held,
        "alpha",
        action="write",
        key="vwap_reclaim",
        text="reclaims held only when volume_ratio > 1.5",
    )

    # A second store is the restart: the note is on disk, not in memory.
    reopened = AgentStore(tmp_path / "agents")
    kept = notes.load(reopened, "alpha")
    assert list(kept) == ["vwap_reclaim"]
    assert "volume_ratio" in kept["vwap_reclaim"]["text"]
    assert kept["vwap_reclaim"]["edits"] == 1


def test_write_replaces_one_key_and_append_extends_it(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.apply(held, "alpha", action="write", key="orl", text="first")

    notes.apply(held, "alpha", action="append", key="orl", text="second")
    assert notes.load(held, "alpha")["orl"]["text"] == "first second"
    assert notes.load(held, "alpha")["orl"]["edits"] == 2

    notes.apply(held, "alpha", action="write", key="orl", text="rewritten")
    assert notes.load(held, "alpha")["orl"]["text"] == "rewritten"
    assert notes.load(held, "alpha")["orl"]["edits"] == 3


def test_append_to_a_missing_key_creates_it(tmp_path: Path) -> None:
    held = store(tmp_path)

    outcome = notes.apply(held, "alpha", action="append", key="new", text="a lesson")

    assert outcome["action"] == "append"
    assert notes.load(held, "alpha")["new"]["text"] == "a lesson"


def test_delete_removes_one_key_and_refuses_an_unknown_one(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.apply(held, "alpha", action="write", key="a", text="one")
    notes.apply(held, "alpha", action="write", key="b", text="two")

    notes.apply(held, "alpha", action="delete", key="a")

    assert list(notes.load(held, "alpha")) == ["b"]
    with pytest.raises(NoteError, match="no note under the key"):
        notes.apply(held, "alpha", action="delete", key="a")


def test_a_key_is_normalised_and_checked(tmp_path: Path) -> None:
    held = store(tmp_path)

    outcome = notes.apply(held, "alpha", action="write", key="  VWAP.Reclaim  ", text="x")

    assert outcome["key"] == "vwap.reclaim"
    with pytest.raises(NoteError, match="not a key"):
        notes.apply(held, "alpha", action="write", key="has spaces", text="x")
    with pytest.raises(NoteError, match="a note needs a key"):
        notes.apply(held, "alpha", action="write", text="x")


def test_a_note_longer_than_its_limit_is_refused_with_the_reason(tmp_path: Path) -> None:
    held = store(tmp_path)

    with pytest.raises(NoteError, match="600"):
        notes.apply(
            held, "alpha", action="write", key="long", text="x" * (notes.MAX_NOTE_CHARS + 1)
        )

    assert notes.load(held, "alpha") == {}


def test_an_append_that_would_overflow_says_to_rewrite_it(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.apply(held, "alpha", action="write", key="k", text="x" * 590)

    with pytest.raises(NoteError, match="rewrite it with write"):
        notes.apply(held, "alpha", action="append", key="k", text="y" * 20)


def test_the_notebook_stops_at_its_key_limit(tmp_path: Path) -> None:
    held = store(tmp_path)
    for number in range(notes.MAX_KEYS):
        notes.apply(held, "alpha", action="write", key=f"k{number}", text="lesson")

    with pytest.raises(NoteError, match="delete a note"):
        notes.apply(held, "alpha", action="write", key="one_too_many", text="lesson")


def test_the_notebook_stops_at_its_total_size(tmp_path: Path) -> None:
    held = store(tmp_path)
    full = notes.MAX_TOTAL_CHARS // notes.MAX_NOTE_CHARS + 1
    for number in range(full):
        try:
            notes.apply(
                held,
                "alpha",
                action="write",
                key=f"k{number}",
                text="x" * notes.MAX_NOTE_CHARS,
            )
        except NoteError as exc:
            assert "characters, past its" in str(exc)
            break
    else:  # pragma: no cover - the limits would have to disagree
        raise AssertionError("the total-size limit never fired")


def test_clear_forgets_everything_and_reports_how_many(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.apply(held, "alpha", action="write", key="a", text="one")
    notes.apply(held, "alpha", action="write", key="b", text="two")

    removed = notes.clear(held, "alpha")

    assert removed == 2
    assert notes.load(held, "alpha") == {}


def test_an_empty_notebook_says_so_rather_than_nothing(tmp_path: Path) -> None:
    digest = notes.digest(store(tmp_path), "alpha")

    assert "empty" in digest


def test_the_digest_names_every_key_until_it_runs_out(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.apply(held, "alpha", action="write", key="orl", text="breaks failed 3 of 4")
    notes.apply(held, "alpha", action="write", key="vwap", text="reclaims need volume")

    digest = notes.digest(held, "alpha")

    assert "orl: breaks failed 3 of 4" in digest
    assert "vwap: reclaims need volume" in digest
    assert "persistent" in digest


def test_a_notebook_too_long_for_the_prompt_names_what_it_cannot_show(
    tmp_path: Path,
) -> None:
    held = store(tmp_path)
    for number in range(notes.MAX_KEYS):
        notes.apply(
            held, "alpha", action="write", key=f"key{number}", text="y" * 200
        )

    digest = notes.digest(held, "alpha")

    assert len(digest) < notes.MAX_DIGEST_CHARS + 400
    assert "call the notes tool" in digest


def test_the_document_is_what_the_operator_screen_needs(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.apply(held, "alpha", action="write", key="orl", text="breaks failed 3 of 4")

    payload = notes.document(held, "alpha")

    assert payload["count"] == 1
    assert payload["capacity"] == notes.MAX_KEYS
    assert payload["notes"][0]["key"] == "orl"
    assert payload["notes"][0]["edits"] == 1
    assert payload["notes"][0]["updated_at"]


def test_end_of_day_checkpoint_is_a_dated_handoff_for_the_next_session(
    tmp_path: Path,
) -> None:
    held = store(tmp_path)
    notes.apply(
        held, "alpha", action="write", key="orl", text="breaks failed 3 of 4"
    )

    saved = notes.checkpoint_session(
        held,
        "alpha",
        day="2026-10-22",
        cycle_id="cyc_final",
    )
    digest = notes.digest(held, "alpha", day="2026-10-23")

    assert saved["day"] == "2026-10-22"
    assert saved["count"] == 1
    assert "Last end-of-day checkpoint: 2026-10-22" in digest
    assert "breaks failed 3 of 4" in digest
    document = notes.document(held, "alpha")
    assert document["last_checkpoint"]["day"] == "2026-10-22"
    assert document["last_checkpoint"]["cycle_id"] == "cyc_final"


def test_editing_tomorrows_notebook_keeps_yesterdays_checkpoint(tmp_path: Path) -> None:
    held = store(tmp_path)
    notes.checkpoint_session(held, "alpha", day="2026-10-22")

    notes.apply(held, "alpha", action="write", key="new", text="today learned this")

    assert notes.document(held, "alpha")["last_checkpoint"]["day"] == "2026-10-22"


def test_only_a_bounded_window_of_session_checkpoints_is_kept(tmp_path: Path) -> None:
    held = store(tmp_path)
    for day in range(1, notes.MAX_SESSIONS + 3):
        notes.checkpoint_session(
            held,
            "alpha",
            day=f"2026-10-{day:02d}",
        )

    document = notes.document(held, "alpha")

    assert len(document["checkpoints"]) == notes.MAX_SESSIONS
    assert document["checkpoints"][0]["day"] == "2026-10-07"
    assert document["checkpoints"][-1]["day"] == "2026-10-03"

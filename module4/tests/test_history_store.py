"""The archive file: what it keeps, and what it does when it is unreadable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from module4.store import HistoryStore, durable

pytestmark = pytest.mark.unit


def test_an_order_round_trips(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.save({"ord_1": {"order_id": "ord_1", "symbol": "TCS", "status": "open"}})

    loaded = store.load()

    assert loaded["ord_1"]["symbol"] == "TCS"
    assert store.meta()["count"] == 1


def test_an_archive_that_was_never_written_is_empty_not_broken(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)

    assert store.load() == {}
    assert store.meta() == {"present": False, "written_at": None, "count": 0}


def test_an_unreadable_archive_does_not_take_the_module_with_it(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.history_path.write_text("{ not json", encoding="utf-8")

    assert store.load() == {}


def test_only_the_fields_that_are_a_record_are_kept() -> None:
    """A price is a reading, not part of what happened."""

    kept = durable(
        {
            "order_id": "ord_1",
            "exit_reason": "target",
            "last_price": 101.0,
            "unrealised_pnl": 10.0,
            "price_age_seconds": 1.0,
        }
    )

    assert kept == {"order_id": "ord_1", "exit_reason": "target"}


def test_the_written_archive_says_when_and_how_much(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.save({"ord_1": {"order_id": "ord_1"}})

    payload = json.loads(store.history_path.read_text(encoding="utf-8"))

    assert payload["count"] == 1
    assert payload["written_at"].endswith("Z")
    assert store.days() == []


def test_the_days_it_holds_are_listed(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.save(
        {
            "a": {"order_id": "a", "session_date": "2026-09-28"},
            "b": {"order_id": "b", "session_date": "2026-09-29"},
            "c": {"order_id": "c"},
        }
    )

    assert store.days() == ["2026-09-28", "2026-09-29"]

"""The ledger: a checkpoint, and the log that can rebuild it.

The point of two files is that either one alone is enough. These tests break
each of them in turn and check the other still answers.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from module3.orders import fill, order_from_decision
from module3.store import OrderStore
from module3.timeutil import IST

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=IST)


def an_order(decision_id: str = "dec_1", symbol: str = "TCS"):
    return order_from_decision(
        {
            "decision_id": decision_id,
            "agent_id": "alpha",
            "action": "buy",
            "symbol": symbol,
            "quantity": 10,
            "entry_price": 100,
            "target": 105,
            "stop_loss": 98,
            "max_hold_minutes": 60,
            "reason": "a level worth leaning on",
            "created_at": NOW.isoformat(),
        },
        now=NOW,
        order_id=f"ord_{decision_id}",
    )


def test_the_checkpoint_round_trips(tmp_path: Path) -> None:
    store = OrderStore(tmp_path)
    order = fill(an_order(), at=NOW, price=Decimal("100"))
    store.append_event("order_filled", order)
    store.write_checkpoint({order.order_id: order})

    loaded = store.load()

    assert list(loaded) == [order.order_id]
    assert loaded[order.order_id].describe() == order.describe()


def test_a_lost_checkpoint_is_rebuilt_from_the_event_log(tmp_path: Path) -> None:
    """A process killed between the two writes must not lose a trade."""

    store = OrderStore(tmp_path)
    first = an_order("dec_1", "TCS")
    second = fill(an_order("dec_2", "INFY"), at=NOW, price=Decimal("100"))
    store.append_event("order_received", first)
    store.append_event("order_received", second)
    store.append_event("order_filled", second)
    store.write_checkpoint({first.order_id: first, second.order_id: second})

    store.orders_path.unlink()
    loaded = store.load()

    assert set(loaded) == {first.order_id, second.order_id}
    assert loaded[second.order_id].status == "open"
    assert loaded[second.order_id].filled_at == NOW
    assert loaded[first.order_id].status == "pending"


def test_a_torn_final_line_does_not_lose_the_events_before_it(tmp_path: Path) -> None:
    store = OrderStore(tmp_path)
    order = an_order()
    store.append_event("order_received", order)
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-09-21T04:30:00.000Z", "kind": "order_fil')

    events = store.read_events(limit=None)

    assert [event["kind"] for event in events] == ["order_received"]
    assert store.replay()[order.order_id].order_id == order.order_id


def test_read_events_returns_the_tail_oldest_first(tmp_path: Path) -> None:
    store = OrderStore(tmp_path)
    order = an_order()
    for kind in ("order_received", "order_filled", "order_closed"):
        store.append_event(kind, order)

    tail = store.read_events(limit=2)

    assert [event["kind"] for event in tail] == ["order_filled", "order_closed"]


def test_an_event_carries_the_whole_order(tmp_path: Path) -> None:
    """So the log is readable on its own, without the checkpoint beside it."""

    store = OrderStore(tmp_path)
    order = fill(an_order(), at=NOW, price=Decimal("100"))

    event = store.append_event("order_filled", order)

    assert event["order_id"] == order.order_id
    assert event["symbol"] == "TCS"
    assert event["status"] == "open"
    assert event["order"]["filled_at"] == order.describe()["filled_at"]
    assert event["ts"].endswith("Z")


def test_an_unreadable_record_does_not_take_the_ledger_with_it(tmp_path: Path) -> None:
    store = OrderStore(tmp_path)
    good = an_order()
    store.append_event("order_received", good)
    store.orders_path.write_text(
        json.dumps(
            {
                "count": 2,
                "orders": [
                    good.describe(),
                    {"order_id": "ord_broken"},  # missing everything else
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load()

    assert set(loaded) == {good.order_id}


def test_an_empty_directory_is_an_empty_ledger(tmp_path: Path) -> None:
    assert OrderStore(tmp_path).load() == {}
    assert OrderStore(tmp_path).read_events() == []


def test_the_written_checkpoint_is_stamped(tmp_path: Path) -> None:
    store = OrderStore(tmp_path)
    store.write_checkpoint({})

    payload = json.loads(store.orders_path.read_text(encoding="utf-8"))
    assert payload["count"] == 0
    assert payload["written_at"]

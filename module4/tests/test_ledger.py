"""The long record: what it keeps, what it ignores, and what it adds up to."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from module4.ledger import Ledger, totals
from module4.store import HistoryStore

pytestmark = pytest.mark.unit

DAY = "2026-09-29"
YESTERDAY = "2026-09-28"


def an_order(
    order_id: str = "ord_1",
    *,
    agent_id: str = "alpha",
    symbol: str = "TCS",
    status: str = "open",
    day: str = DAY,
    gross: float | None = None,
    r_multiple: float | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A row in the shape Module 3 describes, live fields and all."""

    row: dict[str, Any] = {
        "order_id": order_id,
        "decision_id": f"dec_{order_id}",
        "agent_id": agent_id,
        "symbol": symbol,
        "side": "buy",
        "quantity": 10,
        "entry_price": 100.0,
        "target": 105.0,
        "stop_loss": 98.0,
        "status": status,
        "session_date": day,
        "received_at": f"{day}T04:30:00.000Z",
        "pnl": (
            None
            if gross is None
            else {
                "gross": gross,
                "r_multiple": r_multiple,
                "risk": 20.0,
                "outcome": "win" if gross > 0 else "loss",
            }
        ),
        # The fields that move while an order is open. Module 4 must drop them.
        "last_price": 101.0,
        "last_price_source": "live",
        "last_price_at": f"{day}T04:35:00.000Z",
        "price_age_seconds": 1.0,
        "unrealised_pnl": 10.0,
    }
    row.update(overrides)
    return row


class FakeBook:
    """Module 3's daily book, canned, recording what was asked for."""

    def __init__(self, days: dict[str, list[dict[str, Any]]]) -> None:
        self._days = days
        self.asked: list[str] = []

    def days(self) -> list[str]:
        return sorted(self._days, reverse=True)

    def for_day(self, day: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        self.asked.append(day)
        return list(self._days.get(day, []))


def ledger(tmp_path: Path, book: Any = None) -> Ledger:
    return Ledger(store=HistoryStore(tmp_path), book=book)


# -- what it keeps -------------------------------------------------------------- #


def test_an_order_is_kept_and_survives_a_restart(tmp_path: Path) -> None:
    first = ledger(tmp_path)
    first.ingest([an_order()])

    again = Ledger(store=HistoryStore(tmp_path))

    assert [row["order_id"] for row in again.orders()] == ["ord_1"]


def test_the_same_order_twice_is_kept_once(tmp_path: Path) -> None:
    held = ledger(tmp_path)

    held.ingest([an_order()])
    report = held.ingest([an_order()])

    assert report == {"seen": 1, "added": 0, "updated": 0}
    assert len(held.orders()) == 1


def test_an_order_that_changed_is_updated(tmp_path: Path) -> None:
    held = ledger(tmp_path)
    held.ingest([an_order()])

    report = held.ingest(
        [an_order(status="closed", gross=32.5, r_multiple=1.625)]
    )

    assert report["updated"] == 1
    assert held.orders()[0]["status"] == "closed"
    assert held.orders()[0]["pnl"]["gross"] == 32.5


def test_a_price_moving_is_not_the_order_changing(tmp_path: Path) -> None:
    """The live fields belong to the moment, not to the record."""

    held = ledger(tmp_path)
    held.ingest([an_order()])

    report = held.ingest(
        [an_order(last_price=104.9, unrealised_pnl=49.0, price_age_seconds=0.2)]
    )

    assert report["updated"] == 0
    assert "last_price" not in held.orders()[0]
    assert "unrealised_pnl" not in held.orders()[0]


# -- what it asks for ------------------------------------------------------------ #


def test_a_finished_day_is_read_once_and_the_newest_ones_are_read_again(
    tmp_path: Path,
) -> None:
    """A day older than the newest two cannot change, so it is not asked for."""

    book = FakeBook(
        {
            DAY: [an_order()],
            YESTERDAY: [an_order("ord_0", day=YESTERDAY)],
            "2026-09-25": [an_order("ord_old", day="2026-09-25")],
        }
    )
    held = ledger(tmp_path, book)

    held.sync()
    assert sorted(book.asked) == ["2026-09-25", YESTERDAY, DAY]

    book.asked.clear()
    held.sync()

    # Only the two days that can still be written to.
    assert book.asked == [YESTERDAY, DAY]


def test_a_day_the_archive_never_saw_is_read_whatever_its_age(
    tmp_path: Path,
) -> None:
    """A dashboard switched off for a week catches up rather than missing days."""

    book = FakeBook({DAY: [an_order()]})
    held = ledger(tmp_path, book)
    held.sync()

    # A day appears that is older than everything archived.
    book._days["2026-09-20"] = [an_order("ord_missed", day="2026-09-20")]
    book.asked.clear()
    held.sync()

    assert "2026-09-20" in book.asked
    assert {row["order_id"] for row in held.orders()} == {"ord_1", "ord_missed"}


def test_a_sync_that_cannot_reach_the_book_is_reported(tmp_path: Path) -> None:
    class Broken:
        def days(self) -> list[str]:
            raise RuntimeError("Module 3 is unreachable")

    report = ledger(tmp_path, Broken()).sync()

    assert "unreachable" in report["error"]


def test_without_a_book_a_sync_says_so_rather_than_pretending(tmp_path: Path) -> None:
    assert "skipped" in ledger(tmp_path).sync()


# -- what it adds up to ----------------------------------------------------------- #


def test_totals_count_wins_losses_and_the_average_result() -> None:
    rows = [
        an_order("a", status="closed", gross=30.0, r_multiple=2.0),
        an_order("b", status="closed", gross=10.0, r_multiple=0.5),
        an_order("c", status="closed", gross=-20.0, r_multiple=-1.0),
        an_order("d"),
    ]

    numbers = totals(rows)

    assert numbers["orders"] == 4
    assert numbers["closed"] == 3
    assert numbers["open"] == 1
    assert numbers["realised"] == 20.0
    assert numbers["wins"] == 2
    assert numbers["losses"] == 1
    assert numbers["win_rate"] == 66.7
    assert numbers["average_gross"] == 6.67
    assert numbers["average_r"] == 0.5
    assert numbers["best"] == {"symbol": "TCS", "gross": 30.0}
    assert numbers["worst"] == {"symbol": "TCS", "gross": -20.0}


def test_a_book_with_nothing_closed_has_no_rate_to_report() -> None:
    numbers = totals([an_order()])

    assert numbers["closed"] == 0
    assert numbers["win_rate"] is None
    assert numbers["average_r"] is None
    assert numbers["best"] is None


def test_performance_can_be_asked_for_one_agent_or_one_day(tmp_path: Path) -> None:
    held = ledger(tmp_path)
    held.ingest(
        [
            an_order("a", agent_id="alpha", gross=30.0, status="closed"),
            an_order("b", agent_id="beta", gross=-30.0, status="closed"),
            an_order("c", agent_id="alpha", day=YESTERDAY, gross=5.0, status="closed"),
        ]
    )

    assert held.performance(agent_id="alpha")["realised"] == 35.0
    assert held.performance(agent_id="beta")["realised"] == -30.0
    assert held.performance(day=DAY)["realised"] == 0.0
    assert held.performance(agent_id="alpha", day=DAY)["realised"] == 30.0


def test_the_day_table_is_newest_first(tmp_path: Path) -> None:
    held = ledger(tmp_path)
    held.ingest([an_order(day=YESTERDAY), an_order("ord_2", day=DAY)])

    assert [row["day"] for row in held.days()] == [DAY, YESTERDAY]


def test_the_agent_table_is_best_first(tmp_path: Path) -> None:
    held = ledger(tmp_path)
    held.ingest(
        [
            an_order("a", agent_id="alpha", gross=30.0, status="closed"),
            an_order("b", agent_id="beta", gross=-5.0, status="closed"),
        ]
    )

    assert [row["agent_id"] for row in held.agents()] == ["alpha", "beta"]


def test_the_long_book_can_be_narrowed_by_day_status_and_symbol(
    tmp_path: Path,
) -> None:
    held = ledger(tmp_path)
    held.ingest(
        [
            an_order("a", symbol="TCS"),
            an_order("b", symbol="INFY", status="closed", gross=1.0),
            an_order("c", symbol="TCS", day=YESTERDAY),
        ]
    )

    assert [row["order_id"] for row in held.orders(symbol="tcs")] == ["a", "c"]
    assert [row["order_id"] for row in held.orders(status="closed")] == ["b"]
    assert [row["order_id"] for row in held.orders(day=YESTERDAY)] == ["c"]

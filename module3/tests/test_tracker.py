"""The tracker: intake, fills, exits and the clock, with several orders at once."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from module3.clients import MarketError
from module3.store import OrderStore
from module3.timeutil import IST
from module3.tracker import OrderTracker

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=IST)


def decision(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision_id": "dec_1",
        "agent_id": "alpha",
        "action": "buy",
        "symbol": "TCS",
        "quantity": 96,
        "entry_price": 412.90,
        "target": 416.50,
        "stop_loss": 411.40,
        "max_hold_minutes": 60,
        "reason": "close above VWAP with volume_ratio 3.1",
        "timeframe": "15m",
        "price_source": "live",
        "created_at": (NOW - timedelta(seconds=45)).isoformat(),
    }
    payload.update(overrides)
    return payload


def price_row(
    symbol: str,
    price: float,
    *,
    source: str = "live",
    age: float | None = 1.0,
    at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "last_price": price,
        "price_source": source,
        "price_age_seconds": age,
        "price_at": (at or NOW).isoformat(),
    }


class FakeMarket:
    """Module 1's price read, canned."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.calls: list[list[str]] = []

    def prices(self, symbols: Any) -> dict[str, Any]:
        wanted = [str(symbol).upper() for symbol in symbols]
        self.calls.append(wanted)
        rows = [row for row in self.rows if row["symbol"].upper() in wanted]
        return {"rows": rows, "unavailable": [], "requested": wanted}


class FakeDirectory:
    """Module 2's decision log, canned."""

    def __init__(
        self,
        *,
        agents: tuple[str, ...] = ("alpha",),
        decisions: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        self._agents = agents
        self._decisions = list(decisions or [])
        self._error = error

    def agents(self) -> list[str]:
        if self._error:
            raise MarketError(self._error)
        return list(self._agents)

    def decisions(self, agent_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._decisions)


def build(
    tmp_path: Path, *, rows: list[dict[str, Any]] | None = None, **kwargs: Any
) -> tuple[OrderTracker, OrderStore, FakeMarket]:
    store = OrderStore(tmp_path)
    market = FakeMarket(rows)
    tracker = OrderTracker(store=store, market=market, clock=lambda: NOW, **kwargs)
    return tracker, store, market


# -- intake ------------------------------------------------------------------- #


def test_an_order_waits_for_a_price_when_nothing_is_quoting_it(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path)

    result = tracker.ingest(decision())

    assert result["stored"] is True
    assert result["order"]["status"] == "pending"
    assert tracker.watch_symbols() == ["TCS"]


def test_an_order_fills_at_once_on_a_live_price(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.10)])

    result = tracker.ingest(decision())

    order = result["order"]
    assert order["status"] == "open"
    assert order["filled_at"] is not None
    # Entered at the price the market was publishing, with the price the plan
    # was built on kept beside it.
    assert order["entry_price"] == 413.10
    assert order["decision_entry_price"] == 412.90
    assert order["fill_drift_pct"] == 0.0484
    assert tracker.stats()["fills"] == 1


def test_a_stored_close_does_not_execute_an_order(tmp_path: Path) -> None:
    """A close is a price nobody could have traded at."""

    tracker, _, _ = build(
        tmp_path,
        rows=[price_row("TCS", 413.0, source="session_close", age=None)],
    )

    result = tracker.ingest(decision())

    assert result["order"]["status"] == "pending"


def test_a_stale_tick_is_not_live_enough_to_fill_an_order(tmp_path: Path) -> None:
    tracker, _, _ = build(
        tmp_path, rows=[price_row("TCS", 413.0, age=600.0)], stale_price_seconds=120.0
    )

    result = tracker.ingest(decision())

    assert result["order"]["status"] == "pending"


def test_a_plan_the_market_has_already_run_past_is_not_entered(tmp_path: Path) -> None:
    """Entering above the target is not a trade; there is nothing left to gain."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 420.0)])

    result = tracker.ingest(decision())

    order = result["order"]
    assert order["status"] == "expired"
    assert "past this plan's target at 416.50" in order["exit_note"]
    assert tracker.stats()["fills"] == 0


def test_a_plan_the_market_has_already_fallen_through_is_not_entered(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 410.50)])

    result = tracker.ingest(decision())

    assert result["order"]["status"] == "expired"
    assert "past this plan's stop at 411.40" in result["order"]["exit_note"]


def test_a_stale_plan_is_abandoned_even_when_its_levels_still_bracket(
    tmp_path: Path,
) -> None:
    """A wide plan can still bracket a market that has moved a long way."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 420.0)])
    wide = decision(target=435.00, stop_loss=392.00)

    result = tracker.ingest(wide)

    order = result["order"]
    assert order["status"] == "expired"
    assert "1.72%" in order["exit_note"]
    assert "no longer the levels that were decided on" in order["exit_note"]


def test_the_same_decision_twice_does_not_become_two_orders(tmp_path: Path) -> None:
    """The push from Module 2 and the pull from its log must be safe together."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    first = tracker.ingest(decision())

    second = tracker.ingest(decision())

    assert second["stored"] is False
    assert second["duplicate"] is True
    assert second["order"]["order_id"] == first["order"]["order_id"]
    assert len(tracker.orders()) == 1
    assert tracker.stats()["duplicates"] == 1


def test_standing_aside_is_acknowledged_and_not_stored(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path)

    result = tracker.ingest(
        decision(action="ignore", symbol=None, quantity=0, target=None, stop_loss=None)
    )

    assert result["stood_aside"] is True
    assert result["stored"] is False
    assert tracker.orders() == []


# -- exits -------------------------------------------------------------------- #


def test_a_price_through_the_target_closes_the_order_and_books_the_profit(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())

    changed = tracker.on_prices(
        [price_row("TCS", 417.20, at=NOW + timedelta(minutes=5))]
    )

    assert len(changed) == 1
    order = changed[0]
    assert order["status"] == "closed"
    assert order["exit_reason"] == "target"
    assert order["exit_price"] == 416.50
    # Settled from the entry the order got, 413.00, not the plan's 412.90.
    assert order["pnl"]["gross"] == 336.00
    assert order["pnl"]["risk"] == 153.60
    assert order["pnl"]["r_multiple"] == 2.1875
    assert order["pnl"]["outcome"] == "win"
    assert order["ambiguous_exit"] is False


def test_the_stop_closes_an_order_at_a_loss(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())

    changed = tracker.on_prices([price_row("TCS", 410.05)])

    assert changed[0]["exit_reason"] == "stop_loss"
    assert changed[0]["exit_price"] == 410.05
    assert changed[0]["pnl"]["gross"] == -283.20
    assert changed[0]["pnl"]["outcome"] == "loss"


def test_the_entry_is_the_price_the_market_was_showing(tmp_path: Path) -> None:
    """The plan's price is recorded, but the position is entered at the market's."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 414.20)])

    tracker.ingest(decision())
    order = tracker.orders()[0]

    assert order.entry_price == Decimal("414.20")
    assert order.decision_entry_price == Decimal("412.90")
    assert order.risk_per_share == Decimal("2.80")  # 414.20 - 411.40


def test_the_deadline_closes_an_order_that_never_reaches_a_level(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())

    changed = tracker.sweep(now=NOW + timedelta(minutes=61))

    assert len(changed) == 1
    assert changed[0]["exit_reason"] == "timeout"
    assert changed[0]["exit_price"] == 413.00
    assert tracker.stats()["exits"] == 1


def test_an_order_that_is_never_quoted_expires_with_its_window(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path)
    tracker.ingest(decision())

    changed = tracker.sweep(now=NOW + timedelta(minutes=61))

    assert changed[0]["status"] == "expired"
    assert "never became a position" in changed[0]["exit_note"]


def test_a_quiet_stock_is_asked_about_rather_than_waited_for(tmp_path: Path) -> None:
    """The stream only speaks when something moves, so the clock asks outright."""

    tracker, _, market = build(tmp_path)
    tracker.ingest(decision())
    market.rows.append(price_row("TCS", 413.0))

    report = tracker.refresh_prices()

    assert report["rows"] == 1
    assert tracker.orders()[0].status == "open"


def test_watch_symbols_names_only_what_is_still_live(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    assert tracker.watch_symbols() == ["TCS"]

    tracker.on_prices([price_row("TCS", 417.20)])

    assert tracker.watch_symbols() == []


def test_the_module_shows_what_it_is_being_told_for_each_stock(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 414.50, at=NOW + timedelta(seconds=30))])

    quote = tracker.prices(now=NOW + timedelta(seconds=45))[0]

    assert quote["symbol"] == "TCS"
    assert quote["price"] == 414.50
    assert quote["previous"] == 413.00
    assert quote["source"] == "live"
    # Recomputed here, not echoed: a quiet feed shows as an ageing price.
    assert quote["age_seconds"] == 15.0


def test_a_stock_nothing_is_held_in_is_not_quoted(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    assert [quote["symbol"] for quote in tracker.prices()] == ["TCS"]

    tracker.on_prices([price_row("TCS", 417.20)])  # closes at its target

    assert tracker.prices() == []


def test_a_stored_price_is_quoted_without_an_age_that_means_nothing(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(
        tmp_path, rows=[price_row("TCS", 413.0, source="session_close", age=None)]
    )
    tracker.ingest(decision())

    quote = tracker.prices()[0]

    assert quote["source"] == "session_close"
    assert quote["age_seconds"] is None


# -- several orders at once ---------------------------------------------------- #


def test_several_orders_in_one_stock_are_tracked_independently(
    tmp_path: Path,
) -> None:
    """Two agents that disagree are two positions, never one netted number."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.ingest(
        decision(
            decision_id="dec_2",
            agent_id="beta",
            action="sell",
            target=405.00,
            stop_loss=420.00,
        )
    )

    first = tracker.on_prices([price_row("TCS", 417.20)])

    assert len(first) == 1
    assert first[0]["agent_id"] == "alpha"
    assert first[0]["exit_reason"] == "target"
    statuses = {order.agent_id: order.status for order in tracker.orders()}
    assert statuses == {"alpha": "closed", "beta": "open"}

    second = tracker.on_prices([price_row("TCS", 404.00)])

    assert second[0]["agent_id"] == "beta"
    assert second[0]["exit_reason"] == "target"


def test_orders_in_different_stocks_do_not_wait_for_each_other(tmp_path: Path) -> None:
    tracker, _, _ = build(
        tmp_path, rows=[price_row("TCS", 413.0), price_row("INFY", 1500.0)]
    )
    tracker.ingest(decision())
    tracker.ingest(
        decision(
            decision_id="dec_2",
            symbol="INFY",
            entry_price=1500.0,
            target=1520.0,
            stop_loss=1490.0,
            quantity=10,
        )
    )

    changed = tracker.on_prices([price_row("INFY", 1525.0)])

    assert [order["symbol"] for order in changed] == ["INFY"]
    assert tracker.watch_symbols() == ["TCS"]


# -- what the operator sees ---------------------------------------------------- #


def test_the_view_carries_the_live_price_and_the_mark_to_market(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 415.00, at=NOW + timedelta(seconds=30))])

    row = tracker.view(now=NOW + timedelta(seconds=40))[0]

    assert row["last_price"] == 415.0
    assert row["last_price_source"] == "live"
    assert row["price_age_seconds"] == 10.0
    assert row["unrealised_pnl"] == 192.00  # (415.00 - 413.00) * 96
    assert row["deadline_seconds"] == 3600.0 - 40.0


def test_a_stored_price_is_not_given_an_age_that_means_nothing(tmp_path: Path) -> None:
    tracker, _, _ = build(
        tmp_path, rows=[price_row("TCS", 413.0, source="session_close", age=None)]
    )
    tracker.ingest(decision())

    row = tracker.view()[0]

    assert row["last_price_source"] == "session_close"
    assert row["price_age_seconds"] is None


# -- restart and reconciliation ------------------------------------------------ #


def test_the_ledger_survives_a_restart(tmp_path: Path) -> None:
    first, store, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    first.ingest(decision())
    first.on_prices([price_row("TCS", 417.20)])

    second = OrderTracker(store=store, market=FakeMarket(), clock=lambda: NOW)

    orders = second.orders()
    assert len(orders) == 1
    assert orders[0].status == "closed"
    assert orders[0].exit_reason == "target"
    assert orders[0].pnl is not None
    assert orders[0].pnl.gross == Decimal("336.00")
    assert orders[0].entry_price == Decimal("413.00")


def test_reconciliation_picks_up_a_decision_the_push_missed(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])

    report = tracker.reconcile(FakeDirectory(decisions=[decision()]))

    assert report["considered"] == 1
    assert report["recovered"] == 1
    assert len(tracker.orders()) == 1


def test_reconciliation_leaves_a_decision_from_an_earlier_session_alone(
    tmp_path: Path,
) -> None:
    """Booking it now would invent a position in a market that has since closed."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    stale = decision(decision_id="dec_old")
    stale["created_at"] = (NOW - timedelta(hours=4)).isoformat()

    report = tracker.reconcile(FakeDirectory(decisions=[stale]))

    assert report["skipped_old"] == 1
    assert report["recovered"] == 0
    assert tracker.orders() == []


def test_reconciliation_survives_module_2_being_down(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path)

    report = tracker.reconcile(FakeDirectory(error="Module 2 is unreachable"))

    assert report["recovered"] == 0
    assert "unreachable" in report["failed"][0]["error"]


def test_a_decision_that_cannot_be_an_order_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    broken = decision(decision_id="dec_bad", target=400.00)  # below the entry

    report = tracker.reconcile(FakeDirectory(decisions=[broken]))

    assert report["failed"][0]["decision_id"] == "dec_bad"
    assert "below the entry" in report["failed"][0]["error"]
    assert tracker.orders() == []


# -- the daily book ------------------------------------------------------------ #


DAY = NOW.date().isoformat()


def test_the_daily_book_says_what_the_day_made(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 417.20)])  # closes at its target

    book = tracker.summary(day=DAY)

    assert book["closed"] == 1
    assert book["realised"] == 336.00
    assert book["wins"] == 1
    assert book["losses"] == 0
    assert book["win_rate"] == 100.0
    assert book["best"] == {"symbol": "TCS", "gross": 336.00}
    assert book["open_positions"] == []


def test_the_daily_book_lists_what_is_still_open(tmp_path: Path) -> None:
    """The list that stops an agent buying again what it already holds."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())

    book = tracker.summary()

    assert book["open"] == 1
    position = book["open_positions"][0]
    assert position["symbol"] == "TCS"
    assert position["side"] == "buy"
    assert position["quantity"] == 96
    assert position["entry_price"] == 413.00
    assert position["target"] == 416.50
    assert position["stop_loss"] == 411.40


def test_one_agents_book_is_not_another_agents(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.ingest(decision(decision_id="dec_2", agent_id="beta", symbol="TCS"))

    mine = tracker.summary(agent_id="alpha")
    theirs = tracker.summary(agent_id="beta")

    assert mine["orders"] == 1 and mine["open"] == 1
    assert theirs["orders"] == 1 and theirs["open"] == 1
    assert mine["agent_id"] == "alpha"


def test_the_days_index_lists_each_exchange_day_newest_first(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())

    days = tracker.days()

    assert [row["day"] for row in days] == [DAY]
    assert days[0]["orders"] == 1
    assert days[0]["open"] == 1


def test_a_day_with_nothing_on_it_is_an_empty_book_not_an_error(
    tmp_path: Path,
) -> None:
    tracker, _, _ = build(tmp_path)

    book = tracker.summary(day="2026-01-01")

    assert book["orders"] == 0
    assert book["realised"] == 0.0
    assert book["win_rate"] is None
    assert book["best"] is None


# -- forgetting, once the history has it ---------------------------------------- #


def test_pruning_forgets_finished_orders_from_before_the_day(tmp_path: Path) -> None:
    tracker, store, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 417.20)])  # closes at its target

    report = tracker.prune(before="2026-09-30")

    assert report["removed"] == 1
    assert report["kept"] == 0
    assert report["realised_forgotten"] == 336.00
    assert report["symbols"] == ["TCS"]
    assert tracker.orders() == []
    assert store.load() == {}


def test_pruning_keeps_everything_from_the_day_onwards(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 417.20)])

    report = tracker.prune(before=DAY)

    assert report["removed"] == 0
    assert len(tracker.orders()) == 1


def test_pruning_will_not_touch_a_position_it_is_still_watching(
    tmp_path: Path,
) -> None:
    """An open order is not history, whatever day it was taken on."""

    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())

    report = tracker.prune(before="2026-09-30")

    assert report["removed"] == 0
    assert report["still_holding"] == ["TCS"]
    assert len(tracker.orders()) == 1


def test_pruning_leaves_the_event_log_holding_only_what_it_kept(
    tmp_path: Path,
) -> None:
    """A book of the day that keeps every event is not a book of the day."""

    tracker, store, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 417.20)])
    assert len(store.read_events(limit=None)) > 1

    tracker.prune(before="2026-09-30")

    events = store.read_events(limit=None)
    assert len(events) == 1
    assert events[0]["kind"] == "pruned"
    assert events[0]["removed"] == 1
    assert events[0]["before"] == "2026-09-30"


def test_pruning_forgets_one_order_without_disturbing_another(tmp_path: Path) -> None:
    tracker, _, _ = build(tmp_path, rows=[price_row("TCS", 413.0)])
    tracker.ingest(decision())
    tracker.on_prices([price_row("TCS", 417.20)])  # closed
    tracker.ingest(decision(decision_id="dec_2"))  # still open

    report = tracker.prune(before="2026-09-30")

    assert report["removed"] == 1
    assert [order.order_id for order in tracker.orders()] == [
        tracker.orders()[0].order_id
    ]
    assert tracker.orders()[0].status == "open"

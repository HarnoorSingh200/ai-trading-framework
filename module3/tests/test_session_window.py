"""Module 3 inside the operator's day: nothing opens late, nothing outlives it."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from common.window import IST, TradingWindow
from module3.store import OrderStore
from module3.timeutil import format_utc
from module3.tracker import OrderTracker

pytestmark = pytest.mark.unit

WINDOW = TradingWindow()


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, second, tzinfo=IST)


def decision(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "action": "buy",
        "symbol": "INFY",
        "quantity": 10,
        "entry_price": 100.0,
        "target": 105.0,
        "stop_loss": 98.0,
        "max_hold_minutes": 60,
        "reason": "reclaim of VWAP on volume",
        "decision_id": "dec_1",
        "agent_id": "alpha",
        "created_at": "2026-09-21T14:20:00+05:30",
        "timeframe": "5m",
        "bar_open_time": "2026-09-21T14:15:00+05:30",
    }
    order.update(overrides)
    return order


def price_row(
    price: float, *, source: str = "live", at_time: datetime | None = None
) -> dict[str, Any]:
    """A Module 1 price row, stamped when the caller says it was observed."""

    moment = at_time or at(10, 0, 10)
    return {
        "symbol": "INFY",
        "last_price": price,
        "price_source": source,
        "price_at": moment.isoformat(),
        "price_age_seconds": 2.0,
    }


class FakeMarket:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def prices(self, symbols: list[str]) -> dict[str, Any]:
        held = [row for row in self.rows if row["symbol"] in symbols]
        return {"rows": held, "unavailable": []}


class CapturingNotifier:
    """A notifier that keeps what it was told instead of sending it."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, text: str, *, key: str | None = None, dedupe_seconds: float | None = None) -> bool:
        self.messages.append(text)
        return True

    def describe(self) -> dict[str, Any]:
        return {"enabled": True, "sent": len(self.messages)}

    def close(self) -> None:
        return None


def tracker(tmp_path: Path, *, market: FakeMarket | None = None) -> tuple[OrderTracker, CapturingNotifier]:
    notifier = CapturingNotifier()
    held = OrderTracker(
        store=OrderStore(tmp_path),
        market=market,  # type: ignore[arg-type]
        notifier=notifier,
        window=WINDOW,
    )
    return held, notifier


def test_an_order_that_arrives_after_the_close_is_recorded_and_given_up_on(
    tmp_path: Path,
) -> None:
    held, notifier = tracker(tmp_path)

    result = held.ingest(decision(), now=at(14, 55))

    assert result["stored"] is True
    assert result["late"] is True
    assert result["order"]["status"] == "expired"
    assert "window" in result["order"]["exit_note"]
    assert "Expired" in "\n".join(notifier.messages)


def test_the_holding_time_cannot_outlive_the_framework_close(tmp_path: Path) -> None:
    held, _ = tracker(tmp_path, market=FakeMarket([price_row(100.0)]))

    result = held.ingest(decision(), now=at(14, 20))

    assert result["order"]["status"] == "open"
    # 60 minutes from 14:20 would be 15:20; the framework close is 14:50 and
    # the book says so rather than letting the position outlive the day. The
    # deadline is the close, not the end of the requested hour.
    assert result["order"]["session_close_at"] == format_utc(at(14, 50))
    assert result["order"]["deadline"] == format_utc(at(14, 50))


def test_a_pending_order_expires_at_the_framework_close(tmp_path: Path) -> None:
    """Nothing quotes this stock, so it never fills - and it still has a clock."""

    held, _ = tracker(tmp_path, market=FakeMarket([]))

    held.ingest(decision(max_hold_minutes=60), now=at(14, 40))
    changed = held.sweep(now=at(14, 50))

    assert [order["status"] for order in changed] == ["expired"]


def test_an_open_position_is_closed_at_the_framework_close(tmp_path: Path) -> None:
    held, _ = tracker(tmp_path, market=FakeMarket([price_row(100.0)]))

    held.ingest(decision(max_hold_minutes=60), now=at(14, 20))
    changed = held.sweep(now=at(14, 50))

    assert [order["status"] for order in changed] == ["closed"]
    assert changed[0]["exit_reason"] == "timeout"
    assert changed[0]["exit_price"] == 100.0


def test_every_transition_is_reported_to_the_operator(tmp_path: Path) -> None:
    held, notifier = tracker(tmp_path, market=FakeMarket([price_row(100.0)]))

    held.ingest(decision(), now=at(10, 0))
    held.on_prices([price_row(105.0, at_time=at(10, 30))], now=at(10, 30))

    text = "\n".join(notifier.messages)
    assert "New order" in text
    assert "Filled" in text
    assert "Closed" in text
    assert "+50.00" in text
    assert "win" in text


def test_a_fill_reports_where_the_position_was_actually_taken(tmp_path: Path) -> None:
    held, notifier = tracker(tmp_path, market=FakeMarket([price_row(99.5)]))

    held.ingest(decision(), now=at(10, 0))

    text = "\n".join(notifier.messages)
    assert "entry 99.50" in text
    assert "drift" in text

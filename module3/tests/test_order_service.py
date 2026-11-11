"""The order book, through the HTTP surface the site and Module 2 talk to."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from module3.config import Settings
from module3.service import Module3Runtime, create_app
from module3.store import OrderStore
from module3.timeutil import IST
from module3.tracker import OrderTracker

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=IST)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        module1_url="http://module1",
        module2_url="http://module2",
        sweep_seconds=1.0,
        poll_seconds=15.0,
        reconcile_seconds=30.0,
        reconcile_max_age_minutes=120.0,
        max_fill_drift_pct=1.0,
        stale_price_seconds=120.0,
        http_timeout_seconds=30.0,
    )


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


def price_row(symbol: str, price: float, **overrides: Any) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "last_price": price,
        "price_source": "live",
        "price_age_seconds": 1.0,
        "price_at": NOW.isoformat(),
    }
    row.update(overrides)
    return row


class FakeMarket:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])

    def prices(self, symbols: Any) -> dict[str, Any]:
        wanted = [str(symbol).upper() for symbol in symbols]
        return {
            "rows": [row for row in self.rows if row["symbol"].upper() in wanted],
            "unavailable": [],
            "requested": wanted,
        }


class FakeDirectory:
    def __init__(self, decisions: list[dict[str, Any]] | None = None) -> None:
        self._decisions = list(decisions or [])

    def agents(self) -> list[str]:
        return ["alpha"]

    def decisions(self, agent_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._decisions)


def runtime(
    tmp_path: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    directory: Any = None,
) -> Module3Runtime:
    store = OrderStore(tmp_path)
    market = FakeMarket(rows)
    tracker = OrderTracker(store=store, market=market, clock=lambda: NOW)
    return Module3Runtime(
        settings=_settings(tmp_path),
        store=store,
        tracker=tracker,
        market=market,
        stream=None,
        directory=directory,
    )


def client(state: Module3Runtime) -> TestClient:
    # Not used as a context manager on purpose: the lifespan would start the
    # price subscription, and these tests are about the surface, not the wire.
    return TestClient(create_app(state))


def test_health_says_what_is_tracked(tmp_path: Path) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0)])
    api = client(state)
    api.post("/v1/orders", json=decision())

    payload = api.get("/health").json()

    assert payload["ok"] is True
    assert payload["orders"] == {"tracked": 1, "open": 1, "pending": 0}


def test_an_order_can_be_submitted_and_read_back(tmp_path: Path) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0)])
    api = client(state)

    submitted = api.post("/v1/orders", json=decision()).json()["data"]
    order_id = submitted["order"]["order_id"]

    assert submitted["stored"] is True
    fetched = api.get(f"/v1/orders/{order_id}").json()["data"]["order"]
    assert fetched["symbol"] == "TCS"
    assert fetched["status"] == "open"
    # Entered at the live price Module 1 was publishing, with the price the plan
    # was built on kept beside it.
    assert fetched["entry_price"] == 413.00
    assert fetched["decision_entry_price"] == 412.90


def test_an_order_that_cannot_be_read_is_refused_with_the_field(tmp_path: Path) -> None:
    api = client(runtime(tmp_path))

    response = api.post("/v1/orders", json=decision(target=400.0))

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "ORDER_INVALID"
    assert error["details"]["field"] == "target"
    assert "below the entry" in error["message"]


def test_an_unknown_order_is_a_404_and_not_an_empty_answer(tmp_path: Path) -> None:
    api = client(runtime(tmp_path))

    response = api.get("/v1/orders/ord_nothing")

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "ORDER_NOT_FOUND"


def test_a_duplicate_submission_is_reported_rather_than_stored_twice(
    tmp_path: Path,
) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0)])
    api = client(state)
    api.post("/v1/orders", json=decision())

    again = api.post("/v1/orders", json=decision()).json()["data"]

    assert again["duplicate"] is True
    assert api.get("/v1/orders").json()["data"]["count"] == 1


def test_a_decision_to_stand_aside_is_acknowledged_and_not_stored(
    tmp_path: Path,
) -> None:
    state = runtime(tmp_path)
    api = client(state)

    payload = api.post(
        "/v1/orders",
        json=decision(
            action="ignore", symbol=None, quantity=0, target=None, stop_loss=None
        ),
    ).json()["data"]

    assert payload["stood_aside"] is True
    assert api.get("/v1/orders").json()["data"]["count"] == 0


def test_the_book_can_be_filtered(tmp_path: Path) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0), price_row("INFY", 1500.0)])
    api = client(state)
    api.post("/v1/orders", json=decision())
    api.post(
        "/v1/orders",
        json=decision(
            decision_id="dec_2",
            agent_id="beta",
            symbol="INFY",
            entry_price=1500.0,
            target=1520.0,
            stop_loss=1490.0,
        ),
    )

    by_agent = api.get("/v1/orders?agent_id=beta").json()["data"]
    by_symbol = api.get("/v1/orders?symbol=TCS").json()["data"]
    by_status = api.get("/v1/orders?status=pending").json()["data"]

    assert [row["symbol"] for row in by_agent["orders"]] == ["INFY"]
    assert [row["symbol"] for row in by_symbol["orders"]] == ["TCS"]
    assert by_status["count"] == 0


def test_the_events_endpoint_replays_one_order_life(tmp_path: Path) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0)])
    api = client(state)
    order_id = api.post("/v1/orders", json=decision()).json()["data"]["order"]["order_id"]
    state.tracker.on_prices([price_row("TCS", 417.20)])

    payload = api.get(f"/v1/orders/{order_id}/events").json()["data"]

    assert [event["kind"] for event in payload["events"]] == [
        "order_received",
        "order_filled",
        "order_closed",
    ]
    assert payload["events"][-1]["order"]["exit_reason"] == "target"


def test_the_events_endpoint_refuses_an_unknown_order(tmp_path: Path) -> None:
    api = client(runtime(tmp_path))

    response = api.get("/v1/orders/ord_nothing/events")

    assert response.status_code == 404


def test_reconciliation_can_be_forced_and_says_what_it_found(tmp_path: Path) -> None:
    state = runtime(
        tmp_path,
        rows=[price_row("TCS", 413.0)],
        directory=FakeDirectory([decision()]),
    )
    api = client(state)

    payload = api.post("/v1/orders/reconcile").json()["data"]

    assert payload["recovered"] == 1
    assert api.get("/v1/orders").json()["data"]["count"] == 1


def test_reconciliation_without_module_2_says_so(tmp_path: Path) -> None:
    api = client(runtime(tmp_path))

    response = api.post("/v1/orders/reconcile")

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "MODULE2_UNAVAILABLE"


def test_the_status_route_states_the_posture(tmp_path: Path) -> None:
    state = runtime(tmp_path)

    payload = client(state).get("/v1/status").json()["data"]

    assert payload["service"] == "module_3_order_manager"
    assert payload["settings"]["data_dir"] == str(tmp_path)
    # The one thing that must never be in doubt about this module.
    assert "cannot place an order" in payload["broker"]


def test_the_status_route_carries_the_price_of_each_watched_stock(
    tmp_path: Path,
) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0), price_row("INFY", 1500.0)])
    api = client(state)
    api.post(
        "/v1/orders",
        json=decision(
            decision_id="dec_2",
            symbol="INFY",
            entry_price=1500.0,
            target=1520.0,
            stop_loss=1490.0,
        ),
    )

    payload = api.get("/v1/status").json()["data"]

    assert [quote["symbol"] for quote in payload["prices"]] == ["INFY"]
    assert payload["prices"][0]["price"] == 1500.0
    assert payload["prices"][0]["source"] == "live"


def test_the_daily_book_is_served_for_each_agent(tmp_path: Path) -> None:
    """Module 2 asks this, so it has to answer per agent and per day."""

    state = runtime(tmp_path, rows=[price_row("TCS", 413.0)])
    api = client(state)
    api.post("/v1/orders", json=decision())

    book = api.get("/v1/orders/summary?agent_id=alpha").json()["data"]
    assert book["agent_id"] == "alpha"
    assert book["open"] == 1
    assert book["open_positions"][0]["symbol"] == "TCS"
    # Another agent's book is its own.
    assert api.get("/v1/orders/summary?agent_id=beta").json()["data"]["orders"] == 0

    days = api.get("/v1/orders/days").json()["data"]
    assert days["count"] == 1
    today = days["days"][0]["day"]

    assert api.get(f"/v1/orders?day={today}").json()["data"]["count"] == 1
    assert api.get("/v1/orders?day=2020-01-01").json()["data"]["count"] == 0


def test_a_day_with_nothing_on_it_is_an_empty_book_not_a_404(tmp_path: Path) -> None:
    api = client(runtime(tmp_path))

    response = api.get("/v1/orders/summary?day=2020-01-01")

    assert response.status_code == 200
    assert response.json()["data"]["orders"] == 0


def test_pruning_reports_what_it_forgot(tmp_path: Path) -> None:
    state = runtime(tmp_path, rows=[price_row("TCS", 413.0)])
    api = client(state)
    api.post("/v1/orders", json=decision())
    state.tracker.on_prices([price_row("TCS", 417.20)])

    report = api.post("/v1/orders/prune?before=2026-09-30").json()["data"]

    assert report["removed"] == 1
    assert api.get("/v1/orders").json()["data"]["count"] == 0


def test_pruning_with_a_date_it_cannot_read_is_refused(tmp_path: Path) -> None:
    response = client(runtime(tmp_path)).post("/v1/orders/prune?before=last-week")

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "PRUNE_INVALID"

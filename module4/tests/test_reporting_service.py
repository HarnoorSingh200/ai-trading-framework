"""The dashboard's backend: what it answers, through the real HTTP surface."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from module4.benchmark import Benchmark, BenchmarkStore
from module4.config import Settings
from module4.ledger import Ledger
from module4.service import Module4Runtime, create_app
from module4.store import HistoryStore
from module4.timeutil import IST
from module4.usage import TokenUsage, UsageStore

pytestmark = pytest.mark.unit

DAY = "2026-09-29"


def an_order(
    order_id: str = "ord_1",
    *,
    agent_id: str = "alpha",
    status: str = "open",
    gross: float | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_id": order_id,
        "agent_id": agent_id,
        "symbol": "TCS",
        "side": "buy",
        "quantity": 10,
        "entry_price": 100.0,
        "target": 105.0,
        "stop_loss": 98.0,
        "status": status,
        "session_date": DAY,
        "received_at": f"{DAY}T04:30:00.000Z",
        "pnl": None if gross is None else {"gross": gross, "r_multiple": 1.5},
    }
    row.update(overrides)
    return row


class FakeBook:
    def __init__(self, days: dict[str, list[dict[str, Any]]]) -> None:
        self._days = days

    def days(self) -> list[str]:
        return sorted(self._days, reverse=True)

    def for_day(self, day: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        return list(self._days.get(day, []))


def runtime(tmp_path: Path, *, orders: list[dict[str, Any]] | None = None, book: Any = None):
    settings = Settings(
        environment="test",
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        module3_url="http://module3",
        sync_seconds=0.0,
        http_timeout_seconds=5.0,
    )
    store = HistoryStore(tmp_path)
    held = Ledger(store=store, book=book)
    if orders:
        held.ingest(orders)
    return Module4Runtime(settings=settings, store=store, ledger=held, book=book)


def client(state: Module4Runtime) -> TestClient:
    return TestClient(create_app(state))


def test_health_says_what_the_archive_holds(tmp_path: Path) -> None:
    api = client(runtime(tmp_path, orders=[an_order()]))

    payload = api.get("/health").json()

    assert payload["ok"] is True
    assert payload["archive"] == {"orders": 1, "days": 1}


def test_the_dashboard_can_ask_how_it_has_gone(tmp_path: Path) -> None:
    api = client(
        runtime(
            tmp_path,
            orders=[
                an_order("a", status="closed", gross=30.0),
                an_order("b", status="closed", gross=-10.0),
                an_order("c", agent_id="beta", status="closed", gross=5.0),
            ],
        )
    )

    everything = api.get("/v1/performance").json()["data"]
    mine = api.get("/v1/performance?agent_id=alpha").json()["data"]

    assert everything["realised"] == 25.0
    assert everything["win_rate"] == 66.7
    assert mine["realised"] == 20.0
    assert mine["agent_id"] == "alpha"


def test_the_dashboard_can_fetch_chart_ready_analytics(tmp_path: Path) -> None:
    api = client(
        runtime(
            tmp_path,
            orders=[
                an_order("a", status="closed", gross=30.0),
                an_order("b", status="closed", gross=-10.0),
            ],
        )
    )

    payload = api.get("/v1/analytics?agent_id=alpha").json()["data"]

    assert payload["filters"] == {"agent_id": "alpha", "day": None}
    assert payload["summary"]["realised"] == 20.0
    assert [point["cumulative"] for point in payload["equity"]] == [30.0, 20.0]
    assert payload["agents"][0]["agent_id"] == "alpha"
    assert payload["exits"]


def test_the_day_and_agent_tables_are_served(tmp_path: Path) -> None:
    api = client(runtime(tmp_path, orders=[an_order()]))

    assert api.get("/v1/days").json()["data"]["days"][0]["day"] == DAY
    assert api.get("/v1/agents").json()["data"]["agents"][0]["agent_id"] == "alpha"


def test_the_long_book_can_be_narrowed(tmp_path: Path) -> None:
    api = client(
        runtime(
            tmp_path,
            orders=[an_order("a"), an_order("b", status="closed", gross=1.0)],
        )
    )

    open_only = api.get("/v1/orders?status=open").json()["data"]
    assert open_only["count"] == 1
    assert open_only["orders"][0]["order_id"] == "a"
    assert api.get("/v1/orders?agent_id=beta").json()["data"]["count"] == 0


def test_a_sync_can_be_forced_and_says_what_it_found(tmp_path: Path) -> None:
    book = FakeBook({DAY: [an_order("a"), an_order("b")]})
    api = client(runtime(tmp_path, book=book))

    report = api.post("/v1/sync").json()["data"]

    assert report["added"] == 2
    assert api.get("/v1/orders").json()["data"]["count"] == 2
    # Running it again costs a request and changes nothing.
    assert api.post("/v1/sync").json()["data"]["added"] == 0


def test_a_sync_without_a_book_says_so(tmp_path: Path) -> None:
    response = client(runtime(tmp_path)).post("/v1/sync")

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "MODULE3_UNAVAILABLE"


def test_the_status_route_states_the_posture(tmp_path: Path) -> None:
    payload = client(runtime(tmp_path)).get("/v1/status").json()["data"]

    assert payload["service"] == "module_4_reporting"
    # The one thing that must never be in doubt about this module.
    assert "cannot place an order" in payload["broker"]


class FakeUsageClient:
    """Module 2's usage route, without the agent engine."""

    def __init__(self, reports: dict[str, dict[str, Any]] | None = None) -> None:
        self.reports = dict(reports or {})
        self.calls: list[str] = []

    def usage(self, day: str) -> dict[str, Any]:
        self.calls.append(day)
        return self.reports.get(day, {"day": day, "agents": [], "totals": {}})


def usage_day(day: str = DAY) -> dict[str, Any]:
    agent = {
        "agent_id": "alpha",
        "name": "Alpha",
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
        "model_calls": 12,
        "cycles": 5,
        "tool_calls": 20,
        "decisions": 2,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 200,
        "reasoning_tokens": 40,
        "total_tokens": 1100,
        "models": ["gpt-6-luna"],
    }
    totals = {
        key: value
        for key, value in agent.items()
        if key not in {"agent_id", "name", "model", "reasoning_effort"}
    }
    return {"day": day, "agents": [agent], "totals": totals}


def usage_runtime(
    tmp_path: Path, reports: dict[str, dict[str, Any]] | None = None
) -> tuple[Module4Runtime, FakeUsageClient]:
    held = runtime(tmp_path)
    usage_client = FakeUsageClient(reports or {DAY: usage_day()})
    held.usage = TokenUsage(
        store=UsageStore(tmp_path),
        client=usage_client,
        clock=lambda: datetime(2026, 9, 29, 10, 0, tzinfo=IST),
    )
    return held, usage_client


def test_the_dashboard_can_ask_what_the_agents_spent(tmp_path: Path) -> None:
    held, _ = usage_runtime(tmp_path)
    held.usage.sync(day=DAY)  # type: ignore[union-attr]
    api = client(held)

    payload = api.get("/v1/usage", params={"day": DAY}).json()["data"]

    assert payload["count"] == 1
    day = payload["days"][0]
    assert day["day"] == DAY
    assert day["totals"]["prompt_tokens"] == 1000
    assert day["totals"]["reasoning_tokens"] == 40
    assert day["agents"][0]["agent_id"] == "alpha"


def test_a_token_sync_can_be_forced(tmp_path: Path) -> None:
    held, usage_client = usage_runtime(tmp_path)
    api = client(held)

    response = api.post("/v1/usage/sync")

    assert response.status_code == 200
    # Both days are read; only the one with usage is shown on the dashboard.
    assert response.json()["data"]["days"] == 2
    assert usage_client.calls == ["2026-09-28", "2026-09-29"]
    assert api.get("/v1/usage").json()["data"]["count"] == 1


def test_usage_without_an_agent_engine_says_so(tmp_path: Path) -> None:
    api = client(runtime(tmp_path))

    assert api.get("/v1/usage").json()["data"]["count"] == 0
    assert api.post("/v1/usage/sync").status_code == 503


class FakeBenchmarkClient:
    def __init__(self) -> None:
        self.calls = 0

    def benchmark(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "symbol": "NIFTY 50",
            "interval": "1d",
            "candles": [
                {"date": "2026-10-21", "close": 25100.0},
                {"date": "2026-10-22", "close": 25200.0},
            ],
        }


def test_the_dashboard_can_read_the_benchmark(tmp_path: Path) -> None:
    held = runtime(tmp_path)
    held.benchmark = Benchmark(
        store=BenchmarkStore(tmp_path),
        client=FakeBenchmarkClient(),
        clock=lambda: datetime(2026, 9, 29, 10, 0, tzinfo=IST),
    )
    held.benchmark.sync()  # type: ignore[union-attr]
    api = client(held)

    payload = api.get("/v1/benchmark").json()["data"]

    assert payload["symbol"] == "NIFTY 50"
    assert payload["candles"][-1]["close"] == 25200.0
    assert payload["notional"] == 50_000.0


def test_a_benchmark_that_is_not_configured_answers_empty(tmp_path: Path) -> None:
    payload = client(runtime(tmp_path)).get("/v1/benchmark").json()["data"]

    assert payload["symbol"] is None
    assert payload["candles"] == []
    assert payload["notional"] == 50_000.0

"""The reporting arithmetic that feeds the charts and the scorecard."""

from __future__ import annotations

from typing import Any

import pytest

from module4.analytics import build_analytics, totals

pytestmark = pytest.mark.unit


def an_order(
    order_id: str,
    *,
    agent_id: str = "alpha",
    day: str = "2026-09-29",
    symbol: str = "TCS",
    status: str = "closed",
    gross: float = 10.0,
    r_multiple: float = 1.0,
    risk: float = 20.0,
    side: str = "buy",
    exit_reason: str = "target",
    minute: int = 0,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "agent_id": agent_id,
        "symbol": symbol,
        "side": side,
        "quantity": 10,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "status": status,
        "session_date": day,
        "filled_at": f"{day}T04:30:{minute:02d}.000Z",
        "ended_at": f"{day}T05:30:{minute:02d}.000Z",
        "received_at": f"{day}T04:30:{minute:02d}.000Z",
        "exit_reason": exit_reason,
        "pnl": (
            None
            if status != "closed"
            else {
                "gross": gross,
                "r_multiple": r_multiple,
                "risk": risk,
                "outcome": "win" if gross > 0 else "loss" if gross < 0 else "flat",
            }
        ),
    }


def sample_rows() -> list[dict[str, Any]]:
    return [
        an_order(
            "a1",
            day="2026-09-28",
            gross=30.0,
            r_multiple=2.0,
            minute=1,
        ),
        an_order(
            "a2",
            day="2026-09-28",
            gross=-10.0,
            r_multiple=-1.0,
            risk=20.0,
            exit_reason="stop_loss",
            minute=2,
        ),
        an_order(
            "b1",
            agent_id="beta",
            day="2026-09-28",
            gross=5.0,
            r_multiple=0.5,
            risk=10.0,
            minute=3,
        ),
        an_order(
            "a3",
            day="2026-09-29",
            gross=20.0,
            r_multiple=1.0,
            risk=15.0,
            exit_reason="timeout",
            minute=4,
        ),
        an_order(
            "b2",
            agent_id="beta",
            day="2026-09-29",
            gross=-5.0,
            r_multiple=-0.5,
            risk=10.0,
            exit_reason="timeout",
            side="sell",
            minute=5,
        ),
    ]


def test_totals_keep_undefined_ratios_undefined() -> None:
    numbers = totals([an_order("win", gross=10.0, r_multiple=1.0)])

    assert numbers["profit_factor"] is None
    assert numbers["average_loss"] is None
    assert numbers["average_win"] == 10.0
    assert numbers["average_hold_minutes"] == 60.0


def test_an_empty_archive_returns_empty_collections_and_no_invented_ratio() -> None:
    report = build_analytics([])

    assert report["summary"]["orders"] == 0
    assert report["summary"]["win_rate"] is None
    assert report["summary"]["profit_factor"] is None
    assert report["summary"]["max_drawdown"] == 0.0
    assert report["equity"] == []
    assert report["agents"] == []
    assert report["days"] == []
    assert report["r_distribution"]
    assert all(row["count"] == 0 for row in report["r_distribution"])


def test_analytics_measure_the_run_not_only_the_total() -> None:
    report = build_analytics(sample_rows())
    summary = report["summary"]

    assert summary["realised"] == 40.0
    assert summary["win_rate"] == 60.0
    assert summary["profit_factor"] == 3.67
    assert summary["average_r"] == 0.4
    assert summary["max_drawdown"] == 10.0
    assert summary["green_days"] == 2
    assert summary["day_win_rate"] == 100.0
    assert summary["current_streak"] == {"outcome": "loss", "count": 1}
    assert summary["longest_win_streak"] == 2
    assert [point["cumulative"] for point in report["equity"]] == [30, 20, 25, 45, 40]
    assert [point["trade_index"] for point in report["equity"]] == [0, 1, 2, 3, 4]
    assert report["equity"][0]["side"] == "buy"
    assert report["equity"][0]["r_multiple"] == 2.0
    assert report["equity"][0]["exit_reason"] == "target"


def test_each_agent_gets_its_own_curve_and_drawdown() -> None:
    report = build_analytics(sample_rows())
    agents = {row["agent_id"]: row for row in report["agents"]}
    curves = {row["agent_id"]: row for row in report["agent_curves"]}

    assert agents["alpha"]["realised"] == 40.0
    assert agents["beta"]["realised"] == 0.0
    assert agents["alpha"]["max_drawdown"] == 10.0
    assert agents["beta"]["max_drawdown"] == 5.0
    assert agents["alpha"]["green_days"] == 2
    assert agents["alpha"]["trading_days"] == 2
    assert curves["alpha"]["final_cumulative"] == 40.0
    assert [point["cumulative"] for point in curves["beta"]["points"]] == [5, 0]


def test_analytics_can_be_scoped_to_one_agent_or_day() -> None:
    mine = build_analytics(sample_rows(), agent_id="beta")
    one_day = build_analytics(sample_rows(), day="2026-09-29")

    assert mine["summary"]["orders"] == 2
    assert [row["agent_id"] for row in mine["agents"]] == ["beta"]
    assert mine["summary"]["trading_days"] == 2
    assert one_day["summary"]["orders"] == 2
    assert one_day["summary"]["realised"] == 15.0
    assert [row["day"] for row in one_day["days"]] == ["2026-09-29"]
    assert [row["day"] for row in one_day["context_days"]] == [
        "2026-09-29",
        "2026-09-28",
    ]


def test_distribution_exits_and_sides_share_the_same_closed_set() -> None:
    report = build_analytics(sample_rows())
    distribution = {row["key"]: row["count"] for row in report["r_distribution"]}
    exits = {row["reason"]: row["count"] for row in report["exits"]}
    sides = {row["side"]: row for row in report["sides"]}

    assert sum(distribution.values()) == 5
    assert distribution["m1_0"] == 2
    assert distribution["0_1"] == 1
    assert distribution["ge_2"] == 1
    assert exits == {"stop_loss": 1, "target": 2, "timeout": 2}
    assert sides["buy"]["realised"] == 45.0
    assert sides["sell"]["realised"] == -5.0

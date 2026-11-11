"""The operator's trading window, as Module 2 enforces it.

09:30 to 14:30 for decisions, flat by 14:50: the candle that closes at 14:30 is
still answered (a few seconds later, once Module 1 has built it), and the
holding time of that last order is counted from the candle so it gets the whole
twenty minutes the window promises.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from common.window import IST, TradingWindow
from module2.config import Settings
from module2.decision import check, proposal_from_args
from module2.definition import AgentDefinition
from module2.llm import ScriptedClient
from module2.schedule import first_decision_boundary, next_decision_boundary
from module2.store import AgentStore
from module2.timeutil import to_ist
from module2.worker import AgentWorker

pytestmark = pytest.mark.unit

WINDOW = TradingWindow()


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, second, tzinfo=IST)


def clock_text(value: datetime) -> str:
    return to_ist(value).strftime("%H:%M:%S")


# -- when the next wake is ---------------------------------------------------- #


def test_the_next_wake_is_the_next_boundary_plus_the_settle_buffer() -> None:
    """Started at 09:44 on a 5-minute agent: the 09:45 bar, read at 09:45:05."""

    wake = next_decision_boundary(
        at(9, 44), minutes=5, window=WINDOW, settle_seconds=5.0
    )

    assert wake is not None
    assert clock_text(wake) == "09:45:05"


def test_before_the_open_the_first_wake_is_the_first_bar_inside_the_window() -> None:
    wake = next_decision_boundary(
        at(8, 0), minutes=5, window=WINDOW, settle_seconds=5.0
    )

    assert wake is not None
    assert clock_text(wake) == "09:30:05"


def test_a_bar_that_closes_before_the_window_is_not_the_first_decision() -> None:
    """09:15-09:20 closed before the open; the first decision is the 09:30 bar."""

    first = first_decision_boundary(at(8, 0), minutes=5, window=WINDOW)

    assert first is not None
    assert clock_text(first) == "09:30:00"


def test_a_twenty_minute_agent_waits_for_its_own_grid() -> None:
    """A 20-minute bar cannot end at 09:30: the first one ends at 09:35."""

    wake = next_decision_boundary(
        at(9, 16), minutes=20, window=WINDOW, settle_seconds=5.0
    )

    assert wake is not None
    assert clock_text(wake) == "09:35:05"


def test_the_last_wake_of_the_day_is_the_bar_that_closes_at_the_decision_close() -> None:
    wake = next_decision_boundary(
        at(14, 26), minutes=5, window=WINDOW, settle_seconds=5.0
    )

    assert wake is not None
    assert clock_text(wake) == "14:30:05"


def test_after_the_last_bar_there_is_no_wake_left() -> None:
    assert (
        next_decision_boundary(at(14, 35), minutes=5, window=WINDOW, settle_seconds=5.0)
        is None
    )
    assert (
        next_decision_boundary(at(15, 0), minutes=5, window=WINDOW, settle_seconds=5.0)
        is None
    )


def test_a_daily_agent_has_no_decision_in_an_intraday_window() -> None:
    assert (
        next_decision_boundary(
            at(10, 0), minutes=1440, window=WINDOW, settle_seconds=5.0
        )
        is None
    )


# -- the holding time --------------------------------------------------------- #


def definition(**overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": "alpha",
        "name": "Alpha",
        "system_prompt": "Trade the 5-minute.",
        "timeframe": "5m",
        "max_hold_minutes": 60,
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def bundle() -> dict[str, Any]:
    return {
        "symbol": "INFY",
        "bar": {"open_time": "2026-09-21T14:25:00+05:30", "close": 100.0},
        "day": {"last_price": 100.0, "price_source": "live", "price_age_seconds": 2.0},
    }


def proposal(hold: int) -> Any:
    return proposal_from_args(
        {
            "action": "buy",
            "symbol": "INFY",
            "target": 103.0,
            "stop_loss": 99.0,
            "max_hold_minutes": hold,
            "reason": "reclaim of VWAP on volume",
        }
    )


def test_the_last_order_of_the_day_gets_its_twenty_minutes() -> None:
    """The candle closed at 14:30; twenty minutes reaches the 14:50 close."""

    checked = check(
        proposal(20),
        definition=definition(),
        bundle=bundle(),
        now=at(14, 30, 5),
        session_open=True,
        close_at=at(14, 50),
        bar_close_at=at(14, 30),
    )

    assert checked.ok is True
    assert checked.quantity > 0


def test_an_order_that_would_outlive_the_framework_close_is_refused() -> None:
    checked = check(
        proposal(45),
        definition=definition(),
        bundle=bundle(),
        now=at(14, 30, 5),
        session_open=True,
        close_at=at(14, 50),
        bar_close_at=at(14, 30),
    )

    rules = {problem.rule for problem in checked.problems}
    assert checked.ok is False
    assert "exit_time" in rules
    # The refusal says how much room is left, so the model can correct itself.
    assert "20 minutes" in " ".join(checked.messages)


def test_the_holding_time_is_measured_from_the_candle_not_from_the_answer() -> None:
    """A slow model does not steal the position's minutes."""

    checked = check(
        proposal(20),
        definition=definition(),
        bundle=bundle(),
        now=at(14, 31, 20),
        session_open=True,
        close_at=at(14, 50),
        bar_close_at=at(14, 30),
    )

    assert checked.ok is True


# -- the worker --------------------------------------------------------------- #


class FakeMarket:
    """Module 1, canned: a session that is open, and a bar that is ready."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def market_state(self) -> dict[str, Any]:
        self.calls.append("market_state")
        return {
            "session": {
                "is_open": True,
                "session_open_ist": "09:15",
                "session_close_ist": "15:30",
                "next_open": "2026-09-22T09:15:00+05:30",
            }
        }

    def bar_status(self, interval: str) -> dict[str, Any]:
        self.calls.append("bar_status")
        return {
            "interval": interval,
            "bar_open_time": "2026-09-21T10:30:00+05:30",
            "bar_close_time": "2026-09-21T10:35:00+05:30",
            "symbols": 50,
            "ready": 50,
            "building": 0,
            "waiting": 0,
            "complete": True,
        }

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {}


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "host": "127.0.0.1",
        "port": 0,
        "data_dir": tmp_path,
        "secrets_file": None,
        "module1_url": "http://127.0.0.1:5201",
        "module3_url": "http://127.0.0.1:5203",
        "llm_base_url": "https://api.example.test",
        "model": "deepseek-v4-flash",
        "bar_timeout_seconds": 0.0,
        "bar_ready_fraction": 0.9,
        "http_timeout_seconds": 5.0,
        "ready_timeout_seconds": 1.0,
        "window": WINDOW,
        "candle_settle_seconds": 5.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def build(
    tmp_path: Path, *, now: datetime, market: FakeMarket | None = None
) -> tuple[AgentWorker, ScriptedClient, FakeMarket]:
    store = AgentStore(tmp_path / "agents")
    agent = definition()
    store.save(agent)
    held = market or FakeMarket()
    client = ScriptedClient([])
    worker = AgentWorker(
        agent,
        store=store,
        market=held,  # type: ignore[arg-type]
        client=client,
        settings=settings(tmp_path),
        clock=lambda: now,
        sleeper=lambda _seconds: None,
    )
    return worker, client, held


def test_a_worker_does_not_ask_the_model_before_the_window_opens(tmp_path: Path) -> None:
    worker, client, market = build(tmp_path, now=at(9, 15))

    outcome = worker.step(state={})

    assert outcome is None
    assert client.requests == []
    # It did not even read Module 1: the window refuses before anything else.
    assert "bar_status" not in market.calls


def test_a_worker_does_not_ask_the_model_after_the_window_closes(tmp_path: Path) -> None:
    worker, client, market = build(tmp_path, now=at(14, 45))

    outcome = worker.step(state={})

    assert outcome is None
    assert client.requests == []
    assert "bar_status" not in market.calls


def test_the_worker_knows_when_its_day_is_over(tmp_path: Path) -> None:
    halt = threading.Event()

    early, _, _ = build(tmp_path / "early", now=at(12, 0))
    late, _, _ = build(tmp_path / "late", now=at(14, 40))

    assert early._wait_for_window(halt) is True
    assert late._wait_for_window(halt) is False


def test_the_last_decision_moment_is_the_close_plus_its_grace() -> None:
    window = TradingWindow()

    assert window.last_decision_moment(at(12, 0)) == at(14, 30) + timedelta(
        seconds=window.decision_grace_seconds
    )

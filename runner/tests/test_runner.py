"""The central process's decisions: readiness, the day's clock, the controls."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from common.holidays import HolidayList
from common.telegram import NullNotifier
from common.window import IST, TradingWindow
from runner import schedule as day_schedule
from runner.config import RunnerSettings
from runner.control import ControlState, create_control_app, tail_lines
from runner.schedule import DayState
from runner.supervisor import (
    Supervisor,
    framework_stop_at,
    module2_stop_at,
    probe,
    required_indicator_symbols,
    stabilization_reason,
    wait_until_ready,
)

pytestmark = pytest.mark.unit

WINDOW = TradingWindow()


def at(hour: int, minute: int, second: int = 0, *, day: int = 21) -> datetime:
    # 2026-09-21 is a Monday, so weekday tests do not have to think about it.
    return datetime(2026, 9, day, hour, minute, second, tzinfo=IST)


def settings(tmp_path: Path | None = None, **overrides: Any) -> RunnerSettings:
    base: dict[str, Any] = {
        "root": Path(),
        "python": "python",
        "log_dir": (tmp_path or Path("logs")) / "logs",
        "module1_url": "http://127.0.0.1:5201",
        "module2_url": "http://127.0.0.1:5202",
        "module3_url": "http://127.0.0.1:5203",
        "module4_url": "http://127.0.0.1:5204",
        "control_host": "127.0.0.1",
        "control_port": 5205,
        "window": WINDOW,
        "holidays_file": (tmp_path or Path("logs")) / "nse.json",
        "holidays_seed": (tmp_path or Path("logs")) / "seed.json",
        "stabilize_seconds": 60.0,
        "stabilize_timeout_seconds": 420.0,
        "indicator_ready_fraction": 0.9,
        "day_lead_seconds": 300.0,
        "module2_stop_grace_seconds": 60.0,
        "close_grace_seconds": 20.0,
        "health_seconds": 30.0,
        "restart_limit": 5,
        "restart_delay_seconds": 5.0,
    }
    base.update(overrides)
    return RunnerSettings(**base)


def status(**overrides: Any) -> dict[str, Any]:
    """Module 1's status payload, in the nesting it really uses.

    ``board`` is the board worker's whole report: its inner ``board``, its
    ``feed`` and its token count. ``candles.indicators`` is the indicator
    service's own description of the universe it has computed.
    """

    worker: dict[str, Any] = {
        "board": {"resolved": 50, "history_ready": True},
        "tokens": 50,
        "feed": {"connected": True, "state": "connected"},
    }
    payload: dict[str, Any] = {
        "settings": {"feed_enabled": True},
        "warm": {"requested": 50, "seeded": 50, "already_held": 0, "failed": []},
        "candles": {
            "indicators": {
                "symbols": 50,
                "symbols_done": 50,
                "frames": 400,
                "stale_symbols": 0,
            }
        },
    }
    payload["board"] = worker
    payload.update(overrides)
    return payload


def with_indicators(**changes: Any) -> dict[str, Any]:
    payload = status()
    payload["candles"]["indicators"].update(changes)
    return payload


# -- is Module 1 stable ------------------------------------------------------- #


def test_a_fully_warmed_module_is_stable() -> None:
    assert stabilization_reason(status()) is None


def test_history_still_loading_is_not_stable() -> None:
    payload = status()
    payload["board"]["board"] = {"resolved": 50, "history_ready": False}

    reason = stabilization_reason(payload)

    assert reason is not None and "history" in reason


def test_a_disconnected_feed_is_not_stable() -> None:
    payload = status()
    payload["board"]["feed"] = {"connected": False, "state": "reconnecting:2"}

    reason = stabilization_reason(payload)

    assert reason is not None and "reconnecting:2" in reason


def test_a_warm_up_that_has_not_finished_is_not_stable() -> None:
    reason = stabilization_reason(status(warm=None))

    assert reason is not None and "warm-up" in reason


def test_a_failed_warm_up_is_reported_with_its_own_words() -> None:
    reason = stabilization_reason(status(warm={"error": "KiteError: nope"}))

    assert reason is not None and "KiteError" in reason


def test_a_feed_disabled_start_does_not_need_the_websocket() -> None:
    payload = status(settings={"feed_enabled": False})
    payload["board"]["feed"] = None

    assert stabilization_reason(payload) is None


def test_indicators_that_have_not_been_computed_are_not_stable() -> None:
    payload = status()
    payload["candles"]["indicators"] = {
        "symbols": 50,
        "symbols_done": 0,
        "frames": 0,
        "stale_symbols": 0,
    }

    reason = stabilization_reason(payload)

    assert reason is not None
    assert "0 of 50" in reason


def test_a_missing_indicator_block_is_not_stable() -> None:
    payload = status()
    payload["candles"] = {}

    reason = stabilization_reason(payload)

    assert reason is not None and "indicators" in reason


def test_most_of_the_universe_carrying_indicators_is_enough() -> None:
    # 45 of 50 is the operator's nine tenths: one symbol whose history did not
    # come back must not hold the morning hostage.
    assert stabilization_reason(with_indicators(symbols_done=45)) is None


def test_less_than_the_fraction_keeps_waiting() -> None:
    reason = stabilization_reason(with_indicators(symbols_done=44))

    assert reason is not None and "44 of 50" in reason


def test_an_empty_book_is_never_ready_however_kind_the_fraction() -> None:
    payload = with_indicators(symbols=0, symbols_done=0)

    reason = stabilization_reason(payload, indicator_fraction=0.0)

    assert reason is not None


def test_the_required_symbol_count_rounds_up() -> None:
    assert required_indicator_symbols(50) == 45
    assert required_indicator_symbols(10) == 9
    assert required_indicator_symbols(0) == 1


# -- the day's two shutdowns -------------------------------------------------- #


def test_the_agent_fleet_stops_after_the_last_cycle_can_finish() -> None:
    stop_at = module2_stop_at(at(12, 0), WINDOW, grace_seconds=60.0)

    # 14:30 close + 90s decision grace + 60s for the cycle to finish.
    assert stop_at.hour == 14
    assert stop_at.minute == 32
    assert stop_at.second == 30


def test_the_framework_stops_just_after_its_own_close() -> None:
    stop_at = framework_stop_at(at(12, 0), WINDOW, grace_seconds=20.0)

    assert (stop_at.hour, stop_at.minute, stop_at.second) == (14, 50, 20)


# -- the day's schedule ------------------------------------------------------- #


def advance(state: DayState, moment: datetime, **overrides: Any):
    options: dict[str, Any] = {
        "lead_seconds": 300.0,
        "trading_day": True,
        "module2_stop_grace_seconds": 60.0,
        "close_grace_seconds": 20.0,
    }
    options.update(overrides)
    return day_schedule.advance(state, moment, WINDOW, **options)


def test_before_the_day_starts_nothing_is_started() -> None:
    state, actions = advance(DayState(), at(8, 0))

    assert actions == ()
    assert state.started is False
    assert day_schedule.phase(at(8, 0), WINDOW, state, lead_seconds=300.0) == "pre_market"


def test_the_day_starts_with_a_lead_before_the_first_decision() -> None:
    state, actions = advance(DayState(), at(9, 25))

    assert actions == (day_schedule.ACTION_START_DAY,)
    assert state.started is True
    assert state.module2_stopped is False


def test_the_day_starts_only_once() -> None:
    state, _ = advance(DayState(), at(9, 25))

    again = advance(state, at(9, 26))

    assert again[1] == ()


def test_a_process_started_mid_session_starts_the_day_without_replay() -> None:
    state, actions = advance(DayState(), at(11, 0))

    assert actions == (day_schedule.ACTION_START_DAY,)
    assert state.module2_stopped is False


def test_a_process_started_after_the_decision_window_starts_nothing() -> None:
    state, actions = advance(DayState(), at(14, 40))

    assert actions == ()
    assert state.started is True
    assert state.module2_stopped is True
    assert state.module3_stopped is True
    assert state.resting_reason is not None and "14:30" in state.resting_reason


def test_a_process_started_at_the_decision_close_starts_nothing() -> None:
    state, actions = advance(DayState(), at(14, 30))

    assert actions == ()
    assert state.started is True
    assert state.module2_stopped and state.module3_stopped
    assert state.resting_reason is not None and "14:30" in state.resting_reason


def test_a_process_started_after_the_close_starts_nothing() -> None:
    state, actions = advance(DayState(), at(15, 30))

    assert actions == ()
    assert state.started is True
    assert state.resting_reason is not None and "14:30" in state.resting_reason
    assert state.module2_stopped and state.module3_stopped


def test_the_agent_fleet_is_stopped_after_its_last_moment() -> None:
    state, _ = advance(DayState(), at(10, 0))

    stopped, actions = advance(state, at(14, 32, 30))

    assert actions == (day_schedule.ACTION_STOP_MODULE2,)
    assert stopped.module2_stopped is True
    assert stopped.module3_stopped is False


def test_the_order_book_is_stopped_after_the_close_has_settled() -> None:
    state, _ = advance(DayState(), at(10, 0))
    state, _ = advance(state, at(14, 33))

    closed, actions = advance(state, at(14, 50, 20))

    assert actions == (day_schedule.ACTION_STOP_MODULE3,)
    assert closed.module3_stopped is True


def test_a_typical_evening_is_quiet() -> None:
    state, _ = advance(DayState(), at(9, 30))
    state, _ = advance(state, at(14, 32, 30))
    state, _ = advance(state, at(14, 50, 20))

    state, actions = advance(state, at(16, 0))

    assert actions == ()
    assert state.module2_stopped and state.module3_stopped
    assert day_schedule.phase(at(16, 0), WINDOW, state, lead_seconds=300.0) == "day_over"


def test_a_closed_market_day_rests_the_trading_services() -> None:
    state, actions = advance(DayState(), at(10, 0), trading_day=False)

    assert actions == ()
    assert state.started is True
    assert state.resting_reason is not None and "no session" in state.resting_reason
    assert day_schedule.phase(at(10, 0), WINDOW, state, lead_seconds=300.0) == "closed_today"


def test_a_new_day_resets_the_plan() -> None:
    friday = at(10, 0, day=18)
    monday = at(8, 0, day=21)
    state, _ = advance(DayState(), friday)
    state.module2_stopped = True

    fresh, actions = advance(state, monday)

    assert fresh.day == monday.date()
    assert fresh.started is False
    assert fresh.module2_stopped is False
    assert actions == ()


def test_the_next_start_skips_the_weekend() -> None:
    friday_evening = at(17, 0, day=18)

    upcoming = day_schedule.next_day_start(friday_evening, WINDOW, 300.0)

    assert upcoming.weekday() == 0
    assert (upcoming.hour, upcoming.minute) == (9, 25)


def test_the_next_start_is_today_when_today_has_not_started_yet() -> None:
    upcoming = day_schedule.next_day_start(at(7, 0), WINDOW, 300.0)

    assert upcoming.date() == at(7, 0).date()
    assert (upcoming.hour, upcoming.minute) == (9, 25)


# -- the holiday gate before the trading services start ----------------------- #


def local_holidays(tmp_path: Path, *entries: tuple[str, str]) -> HolidayList:
    path = tmp_path / "nse.json"
    path.write_text(
        json.dumps(
            {"holidays": [{"date": day, "name": name} for day, name in entries]}
        ),
        encoding="utf-8",
    )
    return HolidayList([path])


def test_the_runner_reads_a_holiday_without_module1(
    tmp_path: Path,
) -> None:
    from runner.__main__ import market_day

    holidays = local_holidays(tmp_path, ("2026-09-21", "Test Holiday"))

    market = market_day(
        settings(tmp_path),
        moment=at(10, 0),
        holidays=holidays,
    )

    assert market.trading is False
    assert market.source == "local holiday list"
    assert market.detail is not None and "Test Holiday" in market.detail
    assert market.holidays == frozenset({date(2026, 9, 21)})


def test_a_weekend_is_not_a_trading_day(tmp_path: Path) -> None:
    from runner.__main__ import market_day

    market = market_day(
        settings(tmp_path),
        moment=at(10, 0, day=27),
        holidays=local_holidays(tmp_path),
    )

    assert market.trading is False
    assert market.detail is not None and "weekend" in market.detail


def test_a_weekday_with_no_holiday_is_scheduled_not_skipped(tmp_path: Path) -> None:
    from runner.__main__ import market_day

    market = market_day(
        settings(tmp_path),
        moment=at(10, 0),
        holidays=local_holidays(tmp_path),
    )

    assert market.trading is True
    assert market.source == "local weekday"


def test_a_holiday_rests_the_day_with_its_own_name() -> None:
    reason = "NSE holiday: Holi - no session today"

    state, actions = advance(
        DayState(), at(10, 0), trading_day=False, trading_day_reason=reason
    )

    assert actions == ()
    assert state.resting_reason == reason
    assert state.module2_stopped and state.module3_stopped
    assert day_schedule.phase(at(10, 0), WINDOW, state, lead_seconds=300.0) == "closed_today"


def test_a_resting_day_powers_down_once_module1_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import _shutdown_resting_day

    state = runner_state(tmp_path)
    state.ready = True
    state.settings = settings(tmp_path, self_shutdown=True)
    day = DayState(
        day=at(10, 0).date(),
        trading_day=False,
        started=True,
        module2_stopped=True,
        module3_stopped=True,
        resting_reason="NSE holiday: Test Holiday - no session today",
    )
    called: list[bool] = []
    monkeypatch.setattr(
        "runner.__main__.power_down",
        lambda state_: called.append(True) or {"powered_off": True},
    )

    assert _shutdown_resting_day(state, day) is True
    assert called == [True]
    assert day.shutdown_started is True
    assert _shutdown_resting_day(state, day) is False
    assert called == [True]


def test_the_next_start_skips_a_holiday() -> None:
    # Thursday evening; Friday the 25th is a holiday, so Monday the 28th is next.
    upcoming = day_schedule.next_day_start(
        at(17, 0, day=24), WINDOW, 300.0, skip_days={date(2026, 9, 25)}
    )

    assert upcoming.date() == date(2026, 9, 28)
    assert (upcoming.hour, upcoming.minute) == (9, 25)


def test_the_control_tab_pushes_the_next_start_past_a_known_holiday(
    tmp_path: Path,
) -> None:
    state = runner_state(tmp_path)
    state.ready = True
    state.holidays = frozenset({date(2026, 9, 22)})

    payload = state.snapshot(now=at(17, 0))

    # Monday evening, with Tuesday the 22nd a holiday: Wednesday the 23rd.
    assert "23 Sep" in payload["note"]


# -- waiting for Module 1 ----------------------------------------------------- #


def test_waiting_for_stability_ends_as_soon_as_the_book_is_ready() -> None:
    calls = {"count": 0}

    def fetch() -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] < 3:
            raise httpx.ConnectError("not up yet")
        return status()

    ready, reason, waited = wait_until_ready(
        fetch,
        clock=lambda: at(9, 30),
        sleeper=lambda _seconds: None,
        minimum_seconds=0.0,
        timeout_seconds=1.0,
        poll_seconds=0.01,
    )

    assert ready is True
    assert reason is None
    assert calls["count"] == 3
    assert waited >= 0.0


def test_waiting_for_stability_gives_up_with_the_reason() -> None:
    payload = status()
    payload["board"]["board"] = {"resolved": 50, "history_ready": False}

    ready, reason, _ = wait_until_ready(
        lambda: payload,
        clock=lambda: at(9, 30),
        sleeper=lambda _seconds: None,
        minimum_seconds=0.0,
        timeout_seconds=0.05,
        poll_seconds=0.01,
    )

    assert ready is False
    assert reason is not None and "history" in reason


def test_waiting_for_stability_stops_when_the_operator_asks_it_to() -> None:
    payload = status()
    payload["board"]["board"] = {"resolved": 50, "history_ready": False}

    ready, reason, _ = wait_until_ready(
        lambda: payload,
        clock=lambda: at(9, 30),
        sleeper=lambda _seconds: None,
        minimum_seconds=60.0,
        timeout_seconds=420.0,
        poll_seconds=0.01,
        should_stop=lambda: True,
    )

    assert ready is False
    assert reason == "stopped on request"


# -- health probes ------------------------------------------------------------ #


class FakeClient:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {"ok": True}
        self.calls: list[str] = []

    def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        return httpx.Response(
            self.status_code,
            json=self.payload,
            request=httpx.Request("GET", url),
        )

    def close(self) -> None:
        return None


def test_a_healthy_service_says_so() -> None:
    ok, detail = probe("http://127.0.0.1:5201", client=FakeClient())  # type: ignore[arg-type]

    assert ok is True and detail == "ok"


def test_an_http_error_is_a_failing_health_check_not_an_exception() -> None:
    ok, detail = probe("http://127.0.0.1:5201", client=FakeClient(status_code=503))  # type: ignore[arg-type]

    assert ok is False and detail == "HTTP 503"


def test_a_service_that_says_it_is_not_ok_is_not_healthy() -> None:
    ok, detail = probe(
        "http://127.0.0.1:5201",
        client=FakeClient(payload={"ok": False, "detail": "no session"}),  # type: ignore[arg-type]
    )

    assert ok is False and detail == "no session"


# -- the supervisor's bookkeeping --------------------------------------------- #


class FakePopen:
    """A stand-in for a child process, so supervision can be tested alone."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        #: ``subprocess.run`` reads this back when it builds its result.
        self.args = argv
        self.kwargs = kwargs
        self.pid = 4321
        self.code: int | None = None
        self.terminated = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0

    def kill(self) -> None:  # pragma: no cover - only on a stuck child
        self.code = -9

    def communicate(  # pragma: no cover - used by subprocess.run in the tree kill
        self, input: Any = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        return b"", b""

    def __enter__(self) -> FakePopen:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def fake_popen_factory(children: list[FakePopen]):
    def fake_popen(argv: list[str], **kwargs: Any) -> FakePopen:
        child = FakePopen(argv, **kwargs)
        children.append(child)
        return child

    return fake_popen


def test_a_service_is_started_with_its_module_and_can_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(started))
    supervisor = Supervisor(root=tmp_path, python="python", log_dir=tmp_path / "logs")

    report = supervisor.start("module1")

    assert report["started"] is True
    assert started[0].argv == ["python", "-m", "module1"]
    assert supervisor.running() == {"module1": 4321}
    assert supervisor.restarts("module1") == 0
    assert supervisor.stop("module1") is True
    assert supervisor.running() == {}
    assert supervisor.stop("module1") is False


def test_a_process_that_dies_is_reaped_with_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    supervisor = Supervisor(root=tmp_path, python="python", log_dir=tmp_path / "logs")
    supervisor.start("module1")
    children[0].code = 3

    assert supervisor.reap() == [("module1", 3)]
    assert supervisor.running() == {}


def test_a_started_service_describes_itself_for_the_control_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    supervisor = Supervisor(root=tmp_path, python="python", log_dir=tmp_path / "logs")
    supervisor.start("module1")

    described = supervisor.describe("module1")

    assert described["running"] is True
    assert described["pid"] == 4321
    assert described["restarts"] == 0
    assert described["started_at"] is not None
    assert described["uptime_seconds"] is not None
    assert supervisor.describe("module2")["running"] is False


def test_restarting_a_running_service_is_one_stop_and_one_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    supervisor = Supervisor(root=tmp_path, python="python", log_dir=tmp_path / "logs")
    supervisor.start("module2")
    children[0].pid = 1000
    children[0].code = 0  # dies on the way to the restart

    report = supervisor.restart("module2")

    assert report["started"] is True
    assert len(children) == 2
    assert supervisor.running() == {"module2": 4321}
    assert supervisor.restarts("module2") == 1


# -- the control surface ------------------------------------------------------ #


def control_client(tmp_path: Path, *, restart=None) -> TestClient:
    supervisor = Supervisor(root=tmp_path, python="python", log_dir=tmp_path / "logs")

    def default_restart(name: str) -> dict[str, Any]:
        return {"name": name, "pid": 4321, "started": True, "restarts": 0}

    state = ControlState(
        settings=settings(tmp_path),
        supervisor=supervisor,
        snapshot=lambda: {"phase": "pre_market", "services": {}},
        restart=restart or default_restart,
    )
    return TestClient(create_control_app(state))


def test_the_control_status_is_the_runners_own_snapshot(tmp_path: Path) -> None:
    client = control_client(tmp_path)

    response = client.get("/v1/status")

    assert response.status_code == 200
    assert response.json()["data"]["phase"] == "pre_market"


def test_a_restart_is_asked_of_the_runner_and_not_done_behind_its_back(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def restart(name: str) -> dict[str, Any]:
        calls.append(name)
        return {"name": name, "pid": 7}

    client = control_client(
        tmp_path, restart=restart
    )

    response = client.post("/v1/services/module2/restart")

    assert response.status_code == 200
    assert calls == ["module2"]
    assert response.json()["data"]["service"] == "module2"


def test_restarting_a_service_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    client = control_client(tmp_path)

    response = client.post("/v1/services/module9/restart")

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "SERVICE_UNKNOWN"


def test_the_log_reader_returns_the_tail_of_a_services_file(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "module1.process.log").write_text(
        "\n".join(f"line {number}" for number in range(1, 101)) + "\n",
        encoding="utf-8",
    )
    client = control_client(tmp_path)

    response = client.get("/v1/logs/module1?kind=process&lines=3")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["lines"] == ["line 98", "line 99", "line 100"]
    assert data["exists"] is True


def test_a_log_kind_nobody_writes_is_refused(tmp_path: Path) -> None:
    client = control_client(tmp_path)

    response = client.get("/v1/logs/module1?kind=secrets")

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "LOG_KIND_UNKNOWN"


def test_a_missing_log_is_an_empty_tail_not_an_error(tmp_path: Path) -> None:
    client = control_client(tmp_path)

    response = client.get("/v1/logs/module2")

    assert response.status_code == 200
    assert response.json()["data"]["lines"] == []
    assert response.json()["data"]["exists"] is False


def test_tail_lines_reads_from_the_end_of_a_large_file(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    path.write_text("first\n" + ("x" * 100 + "\n") * 500 + "last\n", encoding="utf-8")

    assert tail_lines(path, 2) == ["x" * 100, "last"]
    assert tail_lines(path, 0) == ["last"]


# -- the runner's own snapshot ------------------------------------------------ #


def runner_state(tmp_path: Path, **overrides: Any):
    from runner.__main__ import RunnerState

    supervisor = Supervisor(root=tmp_path, python="python", log_dir=tmp_path / "logs")
    base: dict[str, Any] = {
        "settings": settings(tmp_path),
        "supervisor": supervisor,
        "notifier": NullNotifier(reason="tests"),
        "started_at": at(8, 0),
    }
    base.update(overrides)
    return RunnerState(**base)


def test_the_snapshot_rests_the_trading_services_before_the_day_starts(
    tmp_path: Path,
) -> None:
    state = runner_state(tmp_path)
    state.ready = True
    state.day = DayState(day=at(8, 0).date(), trading_day=True, started=False)

    payload = state.snapshot(now=at(8, 30))

    assert payload["phase"] == "pre_market"
    assert payload["services"]["module1"]["scheduled"] is True
    assert payload["services"]["module4"]["scheduled"] is True
    assert payload["services"]["module2"]["state"] == "resting"
    assert "09:25" in payload["services"]["module2"]["detail"]
    assert payload["services"]["module3"]["state"] == "resting"


def test_the_archive_is_scheduled_while_module1_warms_up(tmp_path: Path) -> None:
    """Reporting starts with market data, not after it; the Control tab says so."""

    state = runner_state(tmp_path)  # not ready: Module 1 is still warming up

    payload = state.snapshot(now=at(8, 30))

    assert payload["phase"] == "starting"
    module4 = payload["services"]["module4"]
    assert module4["scheduled"] is True
    assert module4["state"] == "restarting"
    assert "alongside Module 1" in module4["detail"]
    # The trading services still wait for Module 1, not for the archive.
    assert payload["services"]["module2"]["scheduled"] is False


class FakeSupervisor:
    """Enough of a supervisor to watch what the runner asks of it."""

    def __init__(self, *, running: set[str] | None = None) -> None:
        self.running_names = set(running or ())
        self.started: list[str] = []

    def start(self, name: str) -> dict[str, Any]:
        self.running_names.add(name)
        self.started.append(name)
        return {"name": name, "pid": 1, "started": True}

    def is_running(self, name: str) -> bool:
        return name in self.running_names


def test_becoming_ready_does_not_start_the_archive_twice(tmp_path: Path) -> None:
    from runner.__main__ import _become_ready

    supervisor = FakeSupervisor(running={"module1", "module4"})
    state = runner_state(tmp_path, supervisor=supervisor)

    _become_ready(state, 12.0)

    assert state.ready is True
    assert state.wait_reason is None
    assert supervisor.started == []


def test_becoming_ready_brings_back_an_archive_stopped_during_warm_up(
    tmp_path: Path,
) -> None:
    from runner.__main__ import _become_ready

    supervisor = FakeSupervisor(running={"module1"})
    state = runner_state(tmp_path, supervisor=supervisor)

    _become_ready(state, 0.0)

    assert supervisor.started == ["module4"]


def test_the_snapshot_market_block_uses_the_runners_own_calendar(
    tmp_path: Path,
) -> None:
    local_holidays(tmp_path, ("2026-09-21", "Test Holiday"))
    state = runner_state(tmp_path)
    state.ready = True
    state.day = DayState(
        day=at(10, 0).date(),
        trading_day=False,
        started=True,
        module2_stopped=True,
        module3_stopped=True,
        resting_reason="NSE holiday: Test Holiday - no session today",
    )

    payload = state.snapshot(now=at(10, 0))

    assert payload["market"]["source"] == "runner local list"
    assert payload["market"]["is_holiday"] is True
    assert payload["market"]["holiday_name"] == "Test Holiday"
    assert payload["market"]["open"] is False
    assert payload["services"]["module1"]["scheduled"] is True
    assert payload["services"]["module4"]["scheduled"] is True
    assert payload["services"]["module2"]["state"] == "resting"
    assert payload["services"]["module3"]["state"] == "resting"


def test_the_snapshot_says_why_a_service_rests_after_the_day(tmp_path: Path) -> None:
    state = runner_state(tmp_path)
    state.ready = True
    state.day = DayState(
        day=at(15, 0).date(),
        trading_day=True,
        started=True,
        module2_stopped=True,
        module3_stopped=True,
        resting_reason="the framework's day closed at 14:50 IST",
    )

    payload = state.snapshot(now=at(15, 0))

    assert payload["phase"] == "day_over"
    assert "14:50" in payload["services"]["module2"]["detail"]
    assert payload["day"]["resting_reason"] == "the framework's day closed at 14:50 IST"


def test_a_given_up_service_is_reported_as_failed(tmp_path: Path) -> None:
    state = runner_state(tmp_path)
    state.ready = True
    state.attempts["module1"] = 6

    payload = state.snapshot(now=at(11, 0))

    assert payload["services"]["module1"]["state"] == "failed"
    assert "Restart" in payload["services"]["module1"]["detail"]


def test_the_note_names_the_day_when_the_next_start_is_not_today(tmp_path: Path) -> None:
    # Saturday 10 October 2026: the next session is Monday the 12th.
    saturday = datetime(2026, 10, 10, 10, 0, tzinfo=IST)
    state = runner_state(tmp_path)
    state.ready = True
    state.day = DayState(
        day=saturday.date(),
        trading_day=False,
        started=True,
        module2_stopped=True,
        module3_stopped=True,
        resting_reason="NSE holiday: Test Holiday - no session today",
    )

    payload = state.snapshot(now=saturday)

    assert payload["phase"] == "closed_today"
    assert "12" in payload["note"]


def test_restarting_module1_after_its_failure_reopens_the_day(tmp_path: Path) -> None:
    state = runner_state(tmp_path)
    state.ready = True
    state.day = DayState(
        day=at(11, 0).date(),
        trading_day=True,
        started=True,
        module2_stopped=True,
        module3_stopped=True,
        resting_reason="Module 1 is not available",
    )
    state.attempts["module1"] = 9

    state.restart("module1")

    assert state.attempts["module1"] == 0
    assert state.day.resting_reason is None
    assert state.day.started is False


# -- the day's two transitions ------------------------------------------------- #


def test_starting_the_day_brings_up_the_book_and_a_fresh_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import _run_day_action

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.ready = True
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        _run_day_action(state, day_schedule.ACTION_START_DAY, client)

    # The book first, so it can receive a decision before an agent makes one.
    assert [child.argv for child in children] == [
        ["python", "-m", "module3"],
        ["python", "-m", "module2"],
    ]
    assert state.supervisor.is_running("module3")
    assert state.supervisor.is_running("module2")


def test_stopping_the_fleet_reads_the_token_usage_before_the_door_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import _run_day_action

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.ready = True
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "data": {"days": 2}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _run_day_action(state, day_schedule.ACTION_STOP_MODULE2, client)

    # Module 2's logs are the only copy of the token counts; read them first.
    assert seen == ["/v1/usage/sync"]
    assert state.supervisor.is_running("module2") is False


def test_closing_the_day_archives_the_book_before_stopping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import _run_day_action

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.ready = True
    state.supervisor.start("module3")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/v1/sync"):
            return httpx.Response(200, json={"data": {"stored": 3}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "orders": 3,
                    "closed": 3,
                    "realised": 10.0,
                    "wins": 2,
                    "losses": 1,
                    "average_r": 0.5,
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _run_day_action(state, day_schedule.ACTION_STOP_MODULE3, client)

    assert calls[0].endswith("/v1/sync")
    assert calls[1].endswith("/v1/orders/summary")
    assert state.supervisor.is_running("module3") is False
    assert "3 orders" in state.closing_note
    assert state.notifier.dropped >= 1


# -- the machine's own shutdown ----------------------------------------------- #


def command_recorder(commands: list[str]):
    """A stand-in for the shutdown command: records it and succeeds."""

    def run(command: str) -> tuple[bool, str | None]:
        commands.append(command)
        return True, None

    return run


class CapturingNotifier(NullNotifier):
    def __init__(self) -> None:
        super().__init__(reason="tests")
        self.messages: list[str] = []

    def notify(
        self,
        text: str,
        *,
        key: str | None = None,
        dedupe_seconds: float | None = None,
    ) -> bool:
        self.messages.append(text)
        return True


def test_closing_the_day_powers_the_machine_off_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import _run_day_action

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.ready = True
    state.settings = settings(tmp_path, self_shutdown=True)
    state.supervisor.start("module1")
    state.supervisor.start("module3")
    state.supervisor.start("module4")
    called: list[str] = []
    monkeypatch.setattr(
        "runner.__main__.power_down",
        lambda state_: called.append("power_down") or {"powered_off": True},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/sync"):
            return httpx.Response(200, json={"data": {"stored": 1}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "orders": 1,
                    "closed": 1,
                    "realised": 5.0,
                    "wins": 1,
                    "losses": 0,
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _run_day_action(state, day_schedule.ACTION_STOP_MODULE3, client)

    assert called == ["power_down"]
    assert state.supervisor.is_running("module3") is False


def test_the_control_process_says_the_market_is_off_before_powering_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import power_down

    state = runner_state(tmp_path)
    state.settings = settings(tmp_path, self_shutdown=True)
    state.notifier = CapturingNotifier()

    report = power_down(state, run_command=command_recorder([]))

    assert report["powered_off"] is True
    assert any("Market is off" in message for message in state.notifier.messages)


def test_a_failed_s3_archive_keeps_the_machine_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import _run_day_action

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.ready = True
    state.settings = settings(tmp_path, self_shutdown=True)
    state.supervisor.start("module3")
    called: list[str] = []
    monkeypatch.setattr(
        "runner.__main__.power_down",
        lambda state_: called.append("power_down") or {"powered_off": True},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/sync"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "seen": 1,
                        "s3": {"error": "S3 is down"},
                    }
                },
            )
        return httpx.Response(200, json={"data": {"orders": 1}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _run_day_action(state, day_schedule.ACTION_STOP_MODULE3, client)

    assert called == []
    assert state.supervisor.is_running("module3") is False


def test_the_self_shutdown_is_off_until_it_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import power_down

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.supervisor.start("module1")
    commands: list[str] = []

    report = power_down(state, run_command=command_recorder(commands))

    assert "skipped" in report
    assert commands == []
    assert state.supervisor.is_running("module1")


def test_the_machine_powers_off_only_after_every_service_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import power_down

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.settings = settings(tmp_path, self_shutdown=True)
    for name in ("module1", "module3", "module4"):
        state.supervisor.start(name)
    commands: list[str] = []

    report = power_down(state, run_command=command_recorder(commands))

    assert report["powered_off"] is True
    assert sorted(report["stopped"]) == ["module1", "module3", "module4"]
    assert state.supervisor.running() == {}
    assert commands == ["sudo shutdown -h now"]


def test_a_service_that_will_not_stop_keeps_the_machine_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.__main__ import power_down

    children: list[FakePopen] = []
    monkeypatch.setattr("runner.supervisor.subprocess.Popen", fake_popen_factory(children))
    state = runner_state(tmp_path)
    state.settings = settings(tmp_path, self_shutdown=True)
    state.supervisor.start("module1")
    monkeypatch.setattr(state.supervisor, "stop", lambda name, **kwargs: False)
    commands: list[str] = []

    report = power_down(state, run_command=command_recorder(commands))

    assert report["powered_off"] is False
    assert report["still_running"] == ["module1"]
    assert commands == []
    # The refusal is said out loud rather than only logged.
    assert state.notifier.dropped >= 1


def test_the_shutdown_command_is_split_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runner.__main__ import _run_shutdown_command

    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("runner.__main__.subprocess.run", fake_run)

    ok, error = _run_shutdown_command('sudo shutdown -h now --reason "end of day"')

    assert ok is True and error is None
    assert seen == [["sudo", "shutdown", "-h", "now", "--reason", "end of day"]]


def test_a_refused_shutdown_command_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runner.__main__ import _run_shutdown_command

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="", stderr="sudo: a password is required"
        )

    monkeypatch.setattr("runner.__main__.subprocess.run", fake_run)

    ok, error = _run_shutdown_command("sudo shutdown -h now")

    assert ok is False
    assert error is not None and "password" in error


def test_an_empty_shutdown_command_is_refused() -> None:
    from runner.__main__ import _run_shutdown_command

    ok, error = _run_shutdown_command("   ")

    assert ok is False and error is not None


def test_the_self_shutdown_settings_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.config import load_settings

    monkeypatch.setenv("FRAMEWORK_SELF_SHUTDOWN", "true")
    monkeypatch.setenv("FRAMEWORK_SHUTDOWN_COMMAND", "sudo poweroff")

    resolved = load_settings(root=tmp_path)

    assert resolved.self_shutdown is True
    assert resolved.shutdown_command == "sudo poweroff"


def test_the_self_shutdown_is_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.config import load_settings

    monkeypatch.delenv("FRAMEWORK_SELF_SHUTDOWN", raising=False)
    monkeypatch.delenv("FRAMEWORK_SHUTDOWN_COMMAND", raising=False)

    resolved = load_settings(root=tmp_path)

    assert resolved.self_shutdown is False
    assert resolved.shutdown_command == "sudo shutdown -h now"


# -- the day's summary -------------------------------------------------------- #


class SummaryClient:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return httpx.Response(
            200, json=self.payload, request=httpx.Request("GET", url)
        )


def test_the_day_summary_is_one_readable_line() -> None:
    from runner.__main__ import day_summary

    client = SummaryClient(
        {
            "data": {
                "orders": 14,
                "closed": 6,
                "realised": 1234.5,
                "wins": 4,
                "losses": 2,
                "average_r": 0.75,
            }
        }
    )

    text = day_summary(settings(), client=client)  # type: ignore[arg-type]

    assert "14 orders" in text
    assert "6 closed" in text
    assert "+1234.50" in text
    assert "4W/2L" in text

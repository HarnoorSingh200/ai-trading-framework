"""The operator's day: when decisions may happen, and when everything is flat."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from common.window import IST, TradingWindow, load, parse_clock

pytestmark = pytest.mark.unit


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, second, tzinfo=IST)


def test_a_clock_is_read_or_falls_back() -> None:
    assert parse_clock("09:30", time(0, 0)) == time(9, 30)
    assert parse_clock("14:30:00", time(0, 0)) == time(14, 30)
    assert parse_clock("", time(9, 15)) == time(9, 15)
    assert parse_clock("not a clock", time(9, 15)) == time(9, 15)
    assert parse_clock("25:99", time(9, 15)) == time(9, 15)


def test_the_default_day_is_the_one_the_operator_asked_for() -> None:
    window = TradingWindow()

    assert window.decision_open == time(9, 30)
    assert window.decision_close == time(14, 30)
    assert window.close == time(14, 50)
    # The last order of the day is the one that reaches the close: 14:30 to
    # 14:50 is twenty minutes, and that is what the prompt promises.
    assert window.last_order_minutes() == 20


def test_decisions_run_from_the_open_to_the_close() -> None:
    window = TradingWindow()

    assert window.allows_decision(at(9, 29, 59)) is False
    assert window.allows_decision(at(9, 30)) is True
    assert window.allows_decision(at(12, 0)) is True
    assert window.allows_decision(at(14, 30)) is True
    # The candle that closes at 14:30 is read a few seconds later, so the
    # moment just after the close still has to be allowed.
    assert window.allows_decision(at(14, 30, 5)) is True
    assert window.allows_decision(at(14, 32)) is False


def test_the_framework_is_closed_at_its_own_close_and_not_before() -> None:
    window = TradingWindow()

    assert window.is_closed(at(14, 49, 59)) is False
    assert window.is_closed(at(14, 50)) is True
    assert window.is_closed(at(20, 0)) is True


def test_the_time_left_is_counted_to_the_framework_close() -> None:
    window = TradingWindow()

    assert window.minutes_until_close(at(14, 30)) == 20
    assert window.minutes_until_close(at(14, 30, 5)) == 19
    assert window.minutes_until_close(at(14, 50, 30)) == 0


def test_seconds_until_open_is_zero_while_the_window_is_open() -> None:
    window = TradingWindow()

    assert window.seconds_until_open(at(8, 0)) == 5400.0
    assert window.seconds_until_open(at(12, 0)) == 0.0
    assert window.seconds_until_open(at(15, 0)) == 0.0
    # Before the open it is exact; the caller caps its own wait.
    assert window.seconds_until_open(at(9, 29)) == 60.0


def test_a_window_is_loaded_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAMEWORK_DECISION_OPEN", "10:00")
    monkeypatch.setenv("FRAMEWORK_DECISION_CLOSE", "13:00")
    monkeypatch.setenv("FRAMEWORK_CLOSE", "13:30")
    monkeypatch.setenv("FRAMEWORK_DECISION_GRACE_SECONDS", "30")

    window = load()

    assert window.decision_open == time(10, 0)
    assert window.decision_close == time(13, 0)
    assert window.close == time(13, 30)
    assert window.decision_grace_seconds == 30
    assert window.last_order_minutes() == 30


def test_a_window_with_its_parts_out_of_order_is_refused_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-applying a broken schedule would trade hours nobody chose."""

    monkeypatch.setenv("FRAMEWORK_DECISION_OPEN", "15:00")
    monkeypatch.setenv("FRAMEWORK_DECISION_CLOSE", "10:00")
    monkeypatch.setenv("FRAMEWORK_CLOSE", "10:30")

    window = load()

    assert window.decision_open == time(9, 30)
    assert window.decision_close == time(14, 30)
    assert window.close == time(14, 50)


def test_describing_a_window_never_promises_more_than_it_says() -> None:
    described = TradingWindow().describe()

    assert described["decision_open_ist"] == "09:30"
    assert described["decision_close_ist"] == "14:30"
    assert described["framework_close_ist"] == "14:50"
    assert "operator policy" in described["note"].lower()


def test_a_grace_after_the_last_bar_covers_the_settle_buffer() -> None:
    """The last candle closes at 14:30 and is read at 14:30:05; a 90-second
    grace is what lets that candle be answered at all."""

    window = TradingWindow()
    last = window.last_decision_moment(at(12, 0))

    assert last == at(14, 30) + timedelta(seconds=90)

"""One agent's cycle: what it decides, what it stores, what it refuses to repeat."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from module2.config import Settings
from module2.definition import AgentDefinition
from module2.llm import Reply, ScriptedClient, ToolCall
from module2.store import AgentStore
from module2.timeutil import IST
from module2.worker import AgentWorker

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 45, 30, tzinfo=IST)
BAR = "2026-09-21T10:30:00+05:30"


class FakeMarket:
    """Module 1, canned: a live price, one stock, one screen."""

    def __init__(
        self,
        *,
        price: float = 100.0,
        source: str = "live",
        is_open: bool = True,
        bar_ready_after: int = 1,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.price = price
        self.source = source
        self.is_open = is_open
        self._bar_reads = 0
        self._bar_ready_after = max(1, bar_ready_after)

    def fields(self, interval: str) -> dict[str, Any]:
        self.calls.append(("fields", interval))
        return {"fields": [{"name": "rsi_14"}]}

    def screen(self, **body: Any) -> dict[str, Any]:
        self.calls.append(("screen", body))
        return {
            "interval": body.get("interval"),
            "rows": [{"symbol": "INFY"}],
            "bar_open_time": BAR,
        }

    def stock(self, symbol: str, interval: str) -> dict[str, Any]:
        self.calls.append(("stock", symbol))
        return {
            "symbol": symbol,
            "bar": {"open_time": BAR, "close": self.price},
            "indicator_bar_open_time": BAR,
            "day": {
                "last_price": self.price,
                "price_source": self.source,
                "price_age_seconds": 2.0,
            },
        }

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        self.calls.append(("candles", symbol))
        return {"candles": []}

    def market_state(self) -> dict[str, Any]:
        self.calls.append(("market_state", None))
        return {
            "session": {
                "is_open": self.is_open,
                "session_open_ist": "09:15",
                "session_close_ist": "15:30",
                "next_open": "2026-09-22T09:15:00+05:30",
            }
        }

    def bar_status(self, interval: str) -> dict[str, Any]:
        """Module 1 saying the bar is finished being built."""

        self.calls.append(("bar_status", interval))
        self._bar_reads += 1
        ready = 50 if self._bar_reads >= self._bar_ready_after else 10
        return {
            "interval": interval,
            "bar_open_time": BAR,
            "bar_close_time": "2026-09-21T10:45:00+05:30",
            "symbols": 50,
            "ready": ready,
            "building": 50 - ready,
            "waiting": 0,
            "complete": ready == 50,
        }

    def status(self) -> dict[str, Any]:
        """The readiness read the loop makes before anything else."""

        self.calls.append(("status", None))
        return {
            "board": {"board": {"resolved": 50, "history_ready": True}},
            "warm": {"requested": 50, "seeded": 50, "failed": []},
        }


def definition(**overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": "alpha",
        "name": "Alpha",
        "system_prompt": "Trade the 15-minute.",
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


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
        # No waiting in tests: the fake book says the bar is ready at once.
        "bar_timeout_seconds": 0.0,
        "bar_ready_fraction": 0.9,
        "http_timeout_seconds": 5.0,
        "ready_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def build(
    tmp_path: Path,
    script: list[Any],
    *,
    market: FakeMarket | None = None,
    sink: Any = None,
    book: Any = None,
    settings_overrides: dict[str, Any] | None = None,
    clock: Any = None,
    client: Any = None,
    **overrides: object,
) -> tuple[AgentWorker, AgentStore, Any, FakeMarket]:
    store = AgentStore(tmp_path / "agents")
    agent = definition(**overrides)
    store.save(agent)
    held = market or FakeMarket()
    client = client if client is not None else ScriptedClient(script)
    worker = AgentWorker(
        agent,
        store=store,
        market=held,  # type: ignore[arg-type]
        client=client,
        settings=settings(tmp_path, **(settings_overrides or {})),
        sink=sink,
        book=book,
        clock=clock or (lambda: NOW),
        sleeper=lambda _seconds: None,
    )
    return worker, store, client, held


def buy(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "action": "buy",
        "symbol": "INFY",
        "target": 103.0,
        "stop_loss": 99.0,
        "max_hold_minutes": 45,
        "reason": "reclaim of VWAP on volume",
    }
    order.update(overrides)
    return order


def note(text: str, *, key: str = "lesson", action: str = "write") -> dict[str, Any]:
    """A notes tool call, as the model would make it."""

    return {"tool": "notes", "arguments": {"action": action, "key": key, "text": text}}


def decisions(store: AgentStore) -> list[dict[str, Any]]:
    return store.read_decisions("alpha")


def kinds(store: AgentStore) -> list[str]:
    return [event["type"] for event in store.read_activity("alpha")]


# -- the happy path ---------------------------------------------------------- #


def test_a_cycle_screens_inspects_and_records_the_order(tmp_path: Path) -> None:
    worker, store, _, market = build(
        tmp_path,
        [
            {"tool": "screen", "arguments": {"where": [{"field": "rsi_14", "op": "<", "value": 30}]}},
            {"tool": "stock", "arguments": {"symbol": "INFY"}},
            buy(),
        ],
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert [name for name, _ in market.calls] == ["screen", "stock"]
    stored = decisions(store)
    assert len(stored) == 1
    assert stored[0]["quantity"] == 250
    assert stored[0]["symbol"] == "INFY"
    assert stored[0]["bar_open_time"] == BAR
    assert stored[0]["agent_id"] == "alpha"
    # The key names the stock as well as the candle: one bar can carry more
    # than one decision now, and two orders from one bar must not share a key.
    assert stored[0]["decision_key"] == f"alpha:15m:{BAR}:INFY"
    assert stored[0]["model"] == "deepseek-v4-flash"
    assert stored[0]["prompt_version"]
    assert "cycle_started" in kinds(store)
    assert kinds(store).count("tool_call") == 2
    assert "decision" in kinds(store)


def test_an_ignore_is_recorded_with_its_reason(tmp_path: Path) -> None:
    worker, store, _, market = build(
        tmp_path, [{"action": "ignore", "reason": "nothing on the shortlist is clean"}]
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    stored = decisions(store)
    assert stored[0]["action"] == "ignore"
    assert stored[0]["reason"] == "nothing on the shortlist is clean"
    assert stored[0]["quantity"] == 0
    assert market.calls == []


# -- refusals and failures --------------------------------------------------- #


def test_a_refusal_goes_back_to_the_model_which_may_then_get_it_right(tmp_path: Path) -> None:
    worker, store, client, _ = build(
        tmp_path,
        [
            buy(target=99.0),  # below the entry: not a target
            buy(),
            note("fixed the target"),
        ],
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    refusals = [
        event for event in store.read_activity("alpha") if event["type"] == "tool_result"
    ]
    assert any("refused" in str(event.get("text", "")) for event in refusals)
    assert decisions(store)[0]["target"] == 103.0
    assert len(client.requests) == 3
    assert outcome.notes_kept is True


def test_prose_alone_does_not_become_a_decision(tmp_path: Path) -> None:
    """The framework reads one answer, and prose is not it."""

    worker, store, _, _ = build(
        tmp_path, ["I think INFY looks strong today.", "Still thinking about it."]
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "no_decision"
    assert decisions(store) == []
    assert "cycle_no_decision" in kinds(store)


# -- the record of what the model was told ----------------------------------- #


def test_a_tool_result_is_recorded_whole(tmp_path: Path) -> None:
    """A result cut short reads as a broken tool rather than a short log."""

    class BigScreen(FakeMarket):
        def screen(self, **body: Any) -> dict[str, Any]:
            self.calls.append(("screen", body))
            return {
                "rows": [
                    {"symbol": f"S{index}", "close": index} for index in range(200)
                ]
            }

    worker, store, _, _ = build(
        tmp_path,
        [{"tool": "screen", "arguments": {}}, {"action": "ignore", "reason": "quiet"}],
        market=BigScreen(),
    )

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    recorded = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "tool_result" and event["tool"] == "screen"
    ][0]
    # The last row is still there, and the payload is longer than the 1200
    # characters the log used to keep of it.
    assert '"S199"' in recorded["text"]
    assert len(recorded["text"]) > 1200
    assert json.loads(recorded["text"])["rows"][-1]["symbol"] == "S199"


def test_the_final_round_tells_the_model_to_answer(tmp_path: Path) -> None:
    """Running out of rounds with nothing decided is worse than a shallow call."""

    worker, store, client, _ = build(
        tmp_path, [{"tool": "fields", "arguments": {}}] * 12
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "no_decision"
    asked = [
        message
        for request in client.requests
        for message in request["messages"]
        if message["role"] == "user" and "last round" in str(message["content"])
    ]
    assert asked, "the model was never told it was out of rounds"


def test_a_provider_that_fails_is_retried_once_and_then_recorded(tmp_path: Path) -> None:
    worker, store, client, _ = build(tmp_path, [RuntimeError("provider exploded"), RuntimeError("again")])

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "failed"
    assert decisions(store) == []
    assert "cycle_failed" in kinds(store)
    assert len(client.requests) == 2


# -- handing the order on ---------------------------------------------------- #


class FakeSink:
    """Stands in for Module 3, and can be made not to answer."""

    def __init__(self, *, delivers: bool = True, raises: Exception | None = None) -> None:
        self._delivers = delivers
        self._raises = raises
        self.seen: list[dict[str, Any]] = []
        self.last_error = "HTTP 503: nothing is listening"

    def deliver(self, decision: dict[str, Any]) -> bool:
        if self._raises is not None:
            raise self._raises
        self.seen.append(dict(decision))
        return self._delivers


def test_a_decision_is_handed_on_after_it_is_recorded(tmp_path: Path) -> None:
    sink = FakeSink()
    worker, store, _, _ = build(tmp_path, [buy()], sink=sink)

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert len(sink.seen) == 1
    # What is handed on is the record that was written, not a second description
    # of the same trade: the two modules share one account of what happened.
    assert sink.seen[0]["decision_id"] == decisions(store)[0]["decision_id"]
    assert sink.seen[0]["quantity"] == 250
    assert "order_forwarded" in kinds(store)


def test_an_order_that_could_not_be_handed_on_is_still_recorded(tmp_path: Path) -> None:
    """A delay, not a loss: Module 3 reconciles against this module's log."""

    sink = FakeSink(delivers=False)
    worker, store, _, _ = build(tmp_path, [buy()], sink=sink)

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert len(decisions(store)) == 1
    assert "order_forward_failed" in kinds(store)
    failure = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "order_forward_failed"
    ][0]
    assert failure["error"] == "HTTP 503: nothing is listening"


def test_a_sink_that_raises_does_not_fail_the_cycle(tmp_path: Path) -> None:
    sink = FakeSink(raises=RuntimeError("sink exploded"))
    worker, store, _, _ = build(tmp_path, [buy()], sink=sink)

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    failure = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "order_forward_failed"
    ][0]
    assert "sink exploded" in failure["error"]


def test_with_nowhere_to_hand_it_the_cycle_is_unchanged(tmp_path: Path) -> None:
    worker, store, _, _ = build(tmp_path, [buy()])

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert len(decisions(store)) == 1
    assert not any(kind.startswith("order_forward") for kind in kinds(store))


def test_standing_aside_is_not_handed_over_and_does_not_claim_to_be(
    tmp_path: Path,
) -> None:
    """An ignore is not an order, so nothing goes to the book - and the feed
    must not say that it did, or an operator goes looking for a position that
    was never taken."""

    sink = FakeSink()
    worker, store, _, _ = build(
        tmp_path, [{"action": "ignore", "reason": "no level with room"}], sink=sink
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert sink.seen == []
    assert decisions(store)[0]["action"] == "ignore"
    assert "decision" in kinds(store)
    assert not any(kind.startswith("order_forward") for kind in kinds(store))


class FakeBook:
    """Module 3, read-only: what this agent already holds and has made."""

    def __init__(
        self,
        summary: dict[str, Any] | None = None,
        *,
        today: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.summaries: list[str] = []
        self._summary = summary
        self._today = today

    def mine(
        self, agent_id: str, *, status: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        self.calls.append((agent_id, status))
        return {
            "count": 1,
            "orders": [{"order_id": "ord_1", "symbol": "INFY", "status": "open"}],
        }

    def summary(self, agent_id: str, *, day: str | None = None) -> dict[str, Any]:
        self.summaries.append(agent_id if day is None else f"{agent_id}:{day}")
        if day is not None and self._today is not None:
            return self._today
        if self._summary is not None:
            return self._summary
        return {
            "orders": 1,
            "open": 1,
            "closed": 0,
            "realised": 0.0,
            "wins": 0,
            "losses": 0,
            "open_positions": [
                {
                    "symbol": "INFY",
                    "side": "buy",
                    "quantity": 10,
                    "entry_price": 100.0,
                    "target": 105.0,
                    "stop_loss": 98.0,
                }
            ],
        }


def test_an_agent_can_look_at_its_own_book_before_it_decides(tmp_path: Path) -> None:
    """Its own history is handed over before it is asked, and scoped to it."""

    book = FakeBook()
    worker, store, _, _ = build(
        tmp_path,
        [{"tool": "orders", "arguments": {"status": "open"}}, buy()],
        book=book,
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    # The worker asks for the agent's own book before the first model call, so
    # the agent does not spend a round finding out what it holds - once for
    # everything it is holding, once for what today has made.
    assert book.summaries[0] == "alpha"
    assert len(book.summaries) == 2
    # ...and the model can ask for the detail itself when it wants it.
    assert book.calls == [("alpha", "open")]
    results = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "tool_result" and event["tool"] == "orders"
    ]
    assert results and results[0]["ok"] is True
    assert "INFY" in results[0]["text"]


def test_the_wake_up_holds_every_open_position_but_only_today_s_results(
    tmp_path: Path,
) -> None:
    """An open position is open whichever day it was taken on; a running total
    is not, because it never resets and so says nothing about today."""

    book = FakeBook(
        # What is held: everything, whatever day it came from.
        summary={
            "orders": 1,
            "open": 1,
            "closed": 0,
            "realised": 0.0,
            "open_positions": [
                {
                    "symbol": "DRREDDY",
                    "side": "buy",
                    "quantity": 32,
                    "entry_price": 1243.5,
                    "target": 1258.33,
                    "stop_loss": 1234.0,
                }
            ],
        },
        # Today's results only.
        today={
            "orders": 3,
            "open": 1,
            "closed": 2,
            "realised": 75.0,
            "wins": 1,
            "losses": 1,
            "average_r": 0.3,
            "open_positions": [],
        },
    )
    worker, _, client, _ = build(
        tmp_path, [{"action": "ignore", "reason": "quiet"}], book=book
    )

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    # It asked twice: once without a day, once for today.
    assert book.summaries[0] == "alpha"
    assert book.summaries[1].startswith("alpha:")

    told = client.requests[0]["messages"][1]["content"]
    assert "DRREDDY" in told, "an open position must never be hidden"
    assert "+75.00" in told, "the result is today's, not the whole history"
    assert "1 up and 1 down" in told


def test_the_wake_up_says_what_the_agent_holds_and_how_its_day_went(
    tmp_path: Path,
) -> None:
    """Both halves of its own memory, handed over before it is asked anything."""

    book = FakeBook(
        {
            "orders": 4,
            "open": 1,
            "closed": 3,
            "realised": 412.3,
            "wins": 2,
            "losses": 1,
            "average_r": 1.4,
            "open_positions": [
                {
                    "symbol": "DRREDDY",
                    "side": "buy",
                    "quantity": 32,
                    "entry_price": 1243.5,
                    "target": 1258.33,
                    "stop_loss": 1234.0,
                }
            ],
        },
        today={
            "orders": 4,
            "open": 1,
            "closed": 3,
            "realised": 412.3,
            "wins": 2,
            "losses": 1,
            "average_r": 1.4,
            "open_positions": [],
        },
    )
    worker, _, client, _ = build(
        tmp_path, [{"action": "ignore", "reason": "quiet"}], book=book
    )

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    told = client.requests[0]["messages"][1]["content"]
    assert "DRREDDY" in told and "×32" in told
    assert "stop 1234.0" in told and "target 1258.33" in told
    assert "+412.30" in told
    assert "2 up and 1 down" in told
    assert "+1.40R" in told


def test_one_candle_can_carry_several_orders(tmp_path: Path) -> None:
    """A shortlist of three that likes two of them places two orders in one turn."""

    from module2.llm import Reply, ToolCall

    answer = Reply(
        ok=True,
        model="scripted",
        tool_calls=(
            ToolCall("c1", "submit_decision", buy(symbol="INFY")),
            ToolCall("c2", "submit_decision", buy(symbol="TCS")),
        ),
    )
    worker, store, _, _ = build(tmp_path, [answer])

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert [record["symbol"] for record in outcome.decisions] == ["INFY", "TCS"]
    stored = decisions(store)
    assert len(stored) == 2
    # One cycle, two orders, each traceable on its own.
    assert {record["cycle_id"] for record in stored} == {outcome.cycle_id}
    assert len({record["decision_key"] for record in stored}) == 2
    assert len({record["decision_id"] for record in stored}) == 2


# -- waiting for a fact, and asking for several things at once ---------------- #


def test_the_agent_waits_for_the_book_rather_than_a_fixed_sleep(
    tmp_path: Path,
) -> None:
    """It asks whether the bar is finished, and starts the moment it is."""

    market = FakeMarket(bar_ready_after=3)
    worker, _, _, _ = build(
        tmp_path,
        [{"action": "ignore", "reason": "quiet"}],
        market=market,
        settings_overrides={"bar_timeout_seconds": 5.0},
    )

    outcome = worker.step(state={})

    assert outcome is not None and outcome.status == "decided"
    reads = [call for call in market.calls if call[0] == "bar_status"]
    assert len(reads) == 3  # it kept asking until the book said yes


class SameTimeMarket(FakeMarket):
    """Three stock calls that can only finish if they are running together."""

    def __init__(self, expected: int) -> None:
        super().__init__()
        self._together = threading.Barrier(expected, timeout=5)

    def stock(self, symbol: str, interval: str) -> dict[str, Any]:
        # A sequential loop would wait here until the barrier broke, and the
        # tool would come back as a failure rather than as data.
        self._together.wait()
        return super().stock(symbol, interval)


def test_a_round_asks_about_its_names_all_at_once(tmp_path: Path) -> None:
    from module2.llm import Reply, ToolCall

    answer = Reply(
        ok=True,
        model="scripted",
        tool_calls=(
            ToolCall("c1", "stock", {"symbol": "INFY"}),
            ToolCall("c2", "stock", {"symbol": "TCS"}),
            ToolCall("c3", "stock", {"symbol": "SBIN"}),
        ),
    )
    worker, store, _, _ = build(
        tmp_path,
        [answer, {"action": "ignore", "reason": "nothing worth it"}],
        market=SameTimeMarket(expected=3),
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    results = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "tool_result" and event["tool"] == "stock"
    ]
    assert len(results) == 3
    assert all(result["ok"] for result in results)


class MovingClock:
    """A clock that moves when the worker sleeps, so a session fits in a test."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class JumpingHalt(threading.Event):
    """A stop flag whose wait moves the clock instead of sleeping."""

    def __init__(self, clock: MovingClock) -> None:
        super().__init__()
        self.clock = clock
        self.waits: list[float] = []

    def wait(self, seconds: float | None = None) -> bool:  # type: ignore[override]
        if seconds:
            self.waits.append(round(float(seconds), 1))
            self.clock.advance(seconds)
        return self.is_set()


class StopsAfterWaits(JumpingHalt):
    """A stop flag that ends the run after a given number of sleeps."""

    def __init__(self, clock: MovingClock, after: int) -> None:
        super().__init__(clock)
        self.after = after

    def wait(self, seconds: float | None = None) -> bool:  # type: ignore[override]
        super().wait(seconds)
        if len(self.waits) >= self.after:
            self.set()
        return self.is_set()


def test_a_fresh_agent_does_not_answer_the_candle_that_closed_before_it(
    tmp_path: Path,
) -> None:
    """A restart at 10:16 waits for the 10:20 boundary, not the 10:15 bar."""

    clock = MovingClock(datetime(2026, 9, 21, 10, 16, tzinfo=IST))
    worker, store, client, _ = build(
        tmp_path,
        [{"action": "ignore", "reason": "quiet"}, note("waited for my boundary")],
        clock=clock,
        timeframe="5m",
    )
    halt = StopsAfterWaits(clock, after=1)

    worker.run(stop=halt)

    assert client.requests == []
    # It started and stopped without ever looking: the cleanest possible proof
    # that no cycle ran before the boundary.
    assert kinds(store) == ["agent_started", "agent_stopped"]
    assert clock.now == datetime(2026, 9, 21, 10, 20, 5, tzinfo=IST)


def test_the_first_cycle_after_a_mid_session_start_is_the_next_boundary(
    tmp_path: Path,
) -> None:
    clock = MovingClock(datetime(2026, 9, 21, 10, 16, tzinfo=IST))
    worker, store, client, _ = build(
        tmp_path,
        [{"action": "ignore", "reason": "quiet"}, note("first full bar")],
        clock=clock,
        timeframe="5m",
    )
    halt = StopsAfterWaits(clock, after=3)

    worker.run(stop=halt)

    # One cycle ran, on the bar that closed after the agent existed, and the
    # same candle was not decided again while it waited for the next one.
    assert len(client.requests) == 2
    assert len(decisions(store)) == 1
    assert kinds(store).count("cycle_started") == 1
    assert clock.now >= datetime(2026, 9, 21, 10, 20, 5, tzinfo=IST)


def test_an_agent_started_after_the_last_decision_leaves_without_waiting(
    tmp_path: Path,
) -> None:
    """The day is over: it says so and stops, rather than sleeping until tomorrow."""

    clock = MovingClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
    worker, store, client, _ = build(tmp_path, [], clock=clock, timeframe="5m")
    halt = StopsAfterWaits(clock, after=1)

    worker.run(stop=halt)

    assert halt.waits == []
    assert client.requests == []
    assert "session_window_closed" in kinds(store)


def test_the_wait_between_two_candles_is_the_whole_gap(tmp_path: Path) -> None:
    """A 5-minute agent at 10:16 waits to 10:20 plus the settle buffer."""

    clock = MovingClock(datetime(2026, 9, 21, 10, 16, tzinfo=IST))
    worker, _, _, _ = build(tmp_path, [], clock=clock, timeframe="5m")
    halt = JumpingHalt(clock)

    worker._sleep_for_next_wake(halt)

    # 10:16 to the 10:20 boundary of a 5-minute agent, in one sleep, plus the
    # five-second settle that gives Module 1 time to close the candle and
    # compute the indicators before anyone reads them.
    assert halt.waits == [245.0]
    assert clock.now == datetime(2026, 9, 21, 10, 20, 5, tzinfo=IST)


def test_an_agent_with_nothing_left_in_the_day_waits_for_the_next_session(
    tmp_path: Path,
) -> None:
    """And looks again every minute rather than trusting one reading all night."""

    clock = MovingClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
    worker, _, _, _ = build(tmp_path, [], clock=clock, timeframe="5m")
    halt = JumpingHalt(clock)

    worker._sleep_for_next_wake(halt)

    assert halt.waits == [60.0]


def test_a_provider_that_fails_once_then_answers_still_decides(tmp_path: Path) -> None:
    worker, store, _, _ = build(tmp_path, [RuntimeError("blip"), {"action": "ignore", "reason": "quiet"}])

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert decisions(store)[0]["action"] == "ignore"


def test_the_tool_budget_ends_the_cycle_without_a_decision(tmp_path: Path) -> None:
    worker, store, _, market = build(
        tmp_path,
        [{"tool": "fields", "arguments": {}} for _ in range(6)],
        max_tool_calls_per_cycle=2,
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "no_decision"
    assert len(market.calls) == 2
    assert "budget" in str([e for e in store.read_activity("alpha") if e["type"] == "budget_exhausted"]).lower()


# -- one candle, one decision ------------------------------------------------ #


def test_the_notebook_is_put_in_front_of_the_model_every_cycle(tmp_path: Path) -> None:
    worker, store, client, _ = build(
        tmp_path,
        [{"action": "ignore", "reason": "quiet"}, note("breaks failed 3 of 4")],
    )
    from module2 import notes as notebook

    notebook.apply(
        store, "alpha", action="write", key="orl", text="reclaims need volume"
    )

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    first = client.requests[0]["messages"]
    assert any("reclaims need volume" in str(message.get("content")) for message in first)
    tools = [tool["function"]["name"] for tool in client.requests[0]["tools"]]
    assert "notes" in tools


def test_a_decision_without_a_note_is_reminded_and_then_recorded(tmp_path: Path) -> None:
    worker, store, client, _ = build(tmp_path, [buy()])

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert outcome.notes_kept is False
    assert "notes_missing" in kinds(store)
    # The reminder is in the conversation the model was given, not only in a log.
    assert any(
        "update your notebook" in str(message.get("content"))
        for request in client.requests
        for message in request["messages"]
    )


def test_the_notebook_write_ends_the_cycle_and_is_recorded(tmp_path: Path) -> None:
    worker, store, client, _ = build(
        tmp_path, [buy(), note("bought the VWAP reclaim, volume confirmed")]
    )

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.notes_kept is True
    assert "notes_missing" not in kinds(store)
    updated = [event for event in store.read_activity("alpha") if event["type"] == "notes_updated"]
    assert updated and updated[0]["action"] == "write"
    assert updated[0]["key"] == "lesson"
    # The decision and the note, then nothing: no extra call to be told "done".
    assert len(client.requests) == 2


def test_the_cycle_records_where_its_time_went(tmp_path: Path) -> None:
    """Latency has to be readable from the log, not guessed from wall clock."""

    worker, store, _, _ = build(tmp_path, [buy(), note("timed")])

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    events = store.read_activity("alpha")
    replies = [event for event in events if event["type"] == "model_reply"]
    results = [event for event in events if event["type"] == "tool_result"]
    assert replies and "latency_ms" in replies[0]
    assert all("tool_calls" in reply for reply in replies)
    assert results and all("ms" in result for result in results)
    finished = [event for event in events if event["type"] == "cycle_finished"]
    assert finished and finished[0]["duration_ms"] >= 0
    assert finished[0]["status"] == "decided"


def test_the_final_cycle_checkpoints_the_notebook_for_tomorrow(tmp_path: Path) -> None:
    worker, store, client, _ = build(
        tmp_path,
        [buy(), note("whole-day lesson: only trade the first reclaim")],
    )
    from module2 import notes as notebook

    worker.cycle(
        bar_open_time=BAR,
        market_state={"is_open": True},
        bar_close_at=datetime(2026, 9, 21, 14, 30, tzinfo=IST),
        last_cycle=True,
    )

    checkpoints = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "session_checkpointed"
    ]
    assert checkpoints and checkpoints[0]["day"] == "2026-09-21"
    assert "whole-day lesson" in notebook.digest(
        store, "alpha", day="2026-09-22"
    )
    assert any(
        "final decision cycle" in str(message.get("content"))
        for message in client.requests[0]["messages"]
    )


def test_the_last_decision_boundary_marks_the_cycle_as_end_of_day(
    tmp_path: Path,
) -> None:
    worker, store, _, _ = build(
        tmp_path,
        [{"action": "ignore", "reason": "closed the day"}, note("day lesson")],
        clock=lambda: datetime(2026, 9, 21, 14, 30, 10, tzinfo=IST),
    )

    outcome = worker.step(state={})

    assert outcome is not None
    finished = [
        event
        for event in store.read_activity("alpha")
        if event["type"] == "cycle_finished"
    ]
    assert finished and finished[-1]["last_cycle"] is True
    assert any(event["type"] == "session_checkpointed" for event in store.read_activity("alpha"))


def test_a_lesson_written_tonight_is_read_tomorrow(tmp_path: Path) -> None:
    worker, store, client, _ = build(
        tmp_path, [note("only take ORL breaks with volume_ratio above 1.5")]
    )

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})
    client.queue({"action": "ignore", "reason": "nothing clean"})
    client.queue(note("still true"))
    worker.cycle(
        bar_open_time="2026-09-22T09:30:00+05:30",
        market_state={"is_open": True},
    )

    second = client.requests[2]["messages"]
    assert any("volume_ratio above 1.5" in str(message.get("content")) for message in second)


def test_a_note_written_with_a_decision_in_one_round_ends_there(tmp_path: Path) -> None:
    """The model may decide and write in the same breath; nothing is owed after."""

    from module2.llm import Reply, ToolCall

    both = Reply(
        ok=True,
        model="scripted",
        text="",
        tool_calls=(
            ToolCall(call_id="call_1", name="submit_decision", arguments=buy()),
            ToolCall(
                call_id="call_2",
                name="notes",
                arguments={"action": "write", "key": "lesson", "text": "one round"},
            ),
        ),
    )
    worker, store, client, _ = build(tmp_path, [both])

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert outcome.notes_kept is True
    # One round carried both, and the cycle ended without asking again.
    assert len(client.requests) == 1
    assert len(decisions(store)) == 1


def test_the_same_candle_is_never_decided_twice(tmp_path: Path) -> None:
    worker, store, client, _ = build(
        tmp_path,
        [
            {"action": "ignore", "reason": "quiet"},
            note("nothing to do on a quiet tape"),
        ],
    )
    state: dict[str, Any] = {}

    first = worker.step(state=state)
    second = worker.step(state=state)

    assert first is not None and first.status == "decided"
    assert second is None
    assert len(client.requests) == 2
    assert len(decisions(store)) == 1


def test_a_closed_session_never_reaches_the_model(tmp_path: Path) -> None:
    """Nothing to decide and nobody to ask: no model call discovers this."""

    worker, store, client, _ = build(
        tmp_path, [{"action": "ignore", "reason": "quiet"}], market=FakeMarket(is_open=False)
    )

    outcome = worker.step(state={})

    assert outcome is None
    assert client.requests == []
    assert decisions(store) == []


def test_a_restart_does_not_decide_a_candle_it_already_handled(tmp_path: Path) -> None:
    worker, store, client, _ = build(tmp_path, [{"action": "ignore", "reason": "quiet"}])
    store.save_state("alpha", {"last_attempted_bar": BAR})

    outcome = worker.step(state=store.load_state("alpha"))

    assert outcome is None
    assert client.requests == []


def test_a_decided_bar_is_written_to_the_state_so_a_restart_knows(tmp_path: Path) -> None:
    worker, store, _, _ = build(tmp_path, [{"action": "ignore", "reason": "quiet"}])

    worker.step(state={})

    assert store.load_state("alpha")["last_attempted_bar"] == BAR


# -- what the wake-up says about the session -------------------------------- #


def test_a_wake_up_that_is_silent_about_the_session_is_not_a_closed_market(
    tmp_path: Path,
) -> None:
    """The bar-close stream is the normal wake-up, and it carries the bar alone.

    Reading a *missing* session as a *closed* one refuses every order placed on
    a live market, and names a rule the market has not broken - which is exactly
    what it did.
    """

    worker, store, client, market = build(tmp_path, [buy()])

    outcome = worker.step(state={})

    assert outcome is not None and outcome.status == "decided"
    assert len(decisions(store)) == 1
    # Not knowing is a reason to ask the module that owns the calendar.
    assert ("market_state", None) in market.calls


def test_an_ignore_records_the_candle_it_was_about(tmp_path: Path) -> None:
    """Standing aside inspects no stock, so the bar has to come from the cycle."""

    worker, store, _, _ = build(tmp_path, [{"action": "ignore", "reason": "quiet"}])

    worker.step(state={})

    assert decisions(store)[0]["bar_open_time"] == BAR


def test_a_client_that_speaks_only_async_is_awaited(tmp_path: Path) -> None:
    """The worker's own door is synchronous; the call it makes is not."""

    class AsyncOnly:
        def __init__(self, replies: list[Reply]) -> None:
            self.replies = list(replies)
            self.calls = 0

        async def acomplete(self, **kwargs: Any) -> Reply:
            self.calls += 1
            return self.replies.pop(0)

    decided = Reply(
        ok=True,
        model="scripted",
        text="",
        tool_calls=(
            ToolCall(call_id="call_1", name="submit_decision", arguments=buy()),
            ToolCall(
                call_id="call_2",
                name="notes",
                arguments={"action": "write", "key": "lesson", "text": "one round"},
            ),
        ),
    )
    client = AsyncOnly([decided])
    worker, store, _, _ = build(tmp_path, [], client=client)

    outcome = worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    assert outcome.status == "decided"
    assert client.calls == 1
    assert len(decisions(store)) == 1


def test_a_model_reply_records_cache_and_reasoning_tokens(tmp_path: Path) -> None:
    """The two numbers the bill is made of are written where the log can read them."""

    decided = Reply(
        ok=True,
        model="scripted",
        text="",
        tool_calls=(
            ToolCall(call_id="call_1", name="submit_decision", arguments=buy()),
            ToolCall(
                call_id="call_2",
                name="notes",
                arguments={"action": "write", "key": "lesson", "text": "timed"},
            ),
        ),
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
            "reasoning_tokens": 12,
        },
    )
    worker, store, _, _ = build(tmp_path, [], client=ScriptedClient([decided]))

    worker.cycle(bar_open_time=BAR, market_state={"is_open": True})

    replies = [
        event for event in store.read_activity("alpha") if event["type"] == "model_reply"
    ]
    assert replies
    assert replies[0]["prompt_cache_hit_tokens"] == 70
    assert replies[0]["prompt_cache_miss_tokens"] == 30
    assert replies[0]["reasoning_tokens"] == 12

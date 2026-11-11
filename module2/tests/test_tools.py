"""The tools: what the model may ask for, and what it is told when it may not."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from module2.definition import AgentDefinition
from module2.timeutil import IST
from module2.tools import ToolBox, ToolBudgetExceeded

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 10, 45, tzinfo=IST)


class FakeMarket:
    """Records what the toolbox asked Module 1 for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, str] = {}

    def _record(self, name: str, payload: Any) -> Any:
        self.calls.append((name, payload))
        if name in self.fail:
            raise RuntimeError(self.fail[name])
        return payload

    def fields(self, interval: str) -> dict[str, Any]:
        return self._record("fields", {"interval": interval, "fields": []})

    def screen(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("screen", kwargs)

    def stock(self, symbol: str, interval: str) -> dict[str, Any]:
        return self._record(
            "stock",
            {
                "symbol": symbol,
                "day": {"last_price": 100.0, "price_source": "live", "price_age_seconds": 2.0},
                "bar": {"open_time": "2026-09-21T10:30:00+05:30"},
            },
        )

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        return self._record(
            "candles",
            {"symbol": symbol, "interval": interval, "limit": limit, "candles": []},
        )

    def market_state(self) -> dict[str, Any]:
        return self._record("market_state", {"market": {"is_open": True}})


class FakeBook:
    """Stands in for Module 3: this agent's own orders, and nothing else."""

    def __init__(
        self, orders: list[dict[str, Any]] | None = None, *, error: str | None = None
    ) -> None:
        self.orders = list(orders or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def mine(
        self, agent_id: str, *, status: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        self.calls.append({"agent_id": agent_id, "status": status, "limit": limit})
        if self.error:
            raise RuntimeError(self.error)
        rows = [row for row in self.orders if status is None or row["status"] == status]
        return {"count": len(rows[:limit]), "orders": rows[:limit], "stats": {}}


def definition(**overrides: object) -> AgentDefinition:
    payload: dict[str, object] = {
        "agent_id": "alpha",
        "name": "Alpha",
        "system_prompt": "Trade.",
        "timeframe": "15m",
        "context_timeframes": ["1d"],
    }
    payload.update(overrides)
    return AgentDefinition.from_dict(payload)


def box(
    market: FakeMarket | None = None,
    book: FakeBook | None = None,
    **overrides: object,
) -> tuple[ToolBox, FakeMarket]:
    held = market or FakeMarket()
    toolbox = ToolBox(
        market=held,  # type: ignore[arg-type]
        definition=definition(**overrides),
        book=book,
        market_state={"is_open": True},
        clock=lambda: NOW,
    )
    return toolbox, held


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.text)


# -- the interval the model may name ----------------------------------------- #


def test_a_tool_without_an_interval_uses_the_agents_own_timeframe() -> None:
    toolbox, market = box()

    result = toolbox.call("screen", {"where": []})

    assert result.ok is True
    assert market.calls[0][0] == "screen"
    assert market.calls[0][1]["interval"] == "15m"


def test_a_context_timeframe_the_operator_allowed_is_accepted() -> None:
    toolbox, market = box()

    toolbox.call("screen", {"where": [], "interval": "1d"})

    assert market.calls[0][1]["interval"] == "1d"


def test_a_timeframe_the_operator_did_not_allow_is_refused_without_asking_module_1() -> None:
    """A 15m agent looking at 1m is not a decision this framework will take."""

    toolbox, market = box()

    result = toolbox.call("screen", {"where": [], "interval": "1m"})

    assert result.ok is False
    assert "1m" in result.text
    assert "15m" in result.text
    assert market.calls == []


# -- the tools themselves ---------------------------------------------------- #


def test_every_tool_reaches_the_endpoint_it_names() -> None:
    toolbox, market = box()

    toolbox.call("fields", {})
    toolbox.call("screen", {"where": [], "rank_by": "volume_ratio"})
    toolbox.call("stock", {"symbol": "infy"})
    toolbox.call("candles", {"symbol": "INFY", "limit": 20})
    toolbox.call("market_state", {})

    assert [name for name, _ in market.calls] == [
        "fields",
        "screen",
        "stock",
        "candles",
        "market_state",
    ]
    assert market.calls[2][1]["symbol"] == "INFY"
    assert market.calls[3][1]["limit"] == 20


def test_the_candle_limit_is_bounded_before_it_reaches_module_1() -> None:
    """A model asking for a thousand bars would be given a hundred."""

    toolbox, market = box()

    toolbox.call("candles", {"symbol": "INFY", "limit": 5000})

    assert market.calls[0][1]["limit"] <= 200


def test_an_unknown_tool_is_refused_by_name() -> None:
    toolbox, _ = box()

    result = toolbox.call("get_everything", {})

    assert result.ok is False
    assert "get_everything" in result.text


def test_a_market_failure_comes_back_as_a_result_rather_than_ending_the_cycle() -> None:
    """The model is allowed to hear that its tool failed and try something else."""

    market = FakeMarket()
    market.fail["screen"] = "Module 1 is not answering"
    toolbox, _ = box(market)

    result = toolbox.call("screen", {"where": []})

    assert result.ok is False
    assert "Module 1 is not answering" in result.text


def test_the_tool_budget_is_enforced() -> None:
    toolbox, market = box(max_tool_calls_per_cycle=2)

    assert toolbox.call("fields", {}).ok is True
    assert toolbox.call("fields", {}).ok is True
    with pytest.raises(ToolBudgetExceeded):
        toolbox.call("fields", {})
    assert len(market.calls) == 2


# -- the terminal tool ------------------------------------------------------- #


def test_ignoring_ends_the_cycle_immediately() -> None:
    toolbox, market = box()

    result = toolbox.call("submit_decision", {"action": "ignore", "reason": "nothing to do"})

    assert result.terminal is True
    assert result.ok is True
    assert result.decision is not None
    assert result.decision["action"] == "ignore"
    assert market.calls == []


def test_a_well_formed_order_is_sized_and_ends_the_cycle() -> None:
    toolbox, market = box()

    result = toolbox.call(
        "submit_decision",
        {
            "action": "buy",
            "symbol": "INFY",
            "target": 103.0,
            "stop_loss": 99.0,
            "max_hold_minutes": 45,
            "reason": "reclaim of VWAP",
        },
    )

    assert result.terminal is True
    assert result.decision is not None
    assert result.decision["symbol"] == "INFY"
    assert result.decision["quantity"] == 250
    assert result.decision["entry_price"] == 100.0
    assert [name for name, _ in market.calls] == ["stock"]


def test_an_order_that_breaks_a_rule_does_not_end_the_cycle() -> None:
    """The refusal is a tool result, so the model gets to try again."""

    toolbox, _ = box()

    result = toolbox.call(
        "submit_decision",
        {
            "action": "buy",
            "symbol": "INFY",
            "target": 99.0,  # below the entry: not a target, a mistake
            "stop_loss": 98.0,
            "reason": "x",
        },
    )

    assert result.terminal is False
    assert result.ok is False
    assert "target" in result.text.lower()


def test_a_closed_market_refusal_is_reported_to_the_model() -> None:
    toolbox = ToolBox(
        market=FakeMarket(),  # type: ignore[arg-type]
        definition=definition(),
        market_state={"is_open": False},
        clock=lambda: NOW,
    )

    result = toolbox.call(
        "submit_decision",
        {"action": "buy", "symbol": "INFY", "target": 103.0, "stop_loss": 99.0, "reason": "x"},
    )

    assert result.terminal is False
    assert "closed" in result.text.lower()


def test_malformed_arguments_are_refused_rather_than_raising() -> None:
    toolbox, _ = box()

    result = toolbox.call("submit_decision", {"action": "buy"})

    assert result.ok is False
    assert result.terminal is False


# -- the agent's own book ---------------------------------------------------- #


def an_order(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_id": "ord_1",
        "symbol": "DRREDDY",
        "side": "buy",
        "quantity": 32,
        "entry_price": 1243.5,
        "target": 1258.33,
        "stop_loss": 1234.0,
        "status": "open",
        "last_price": 1250.1,
        "unrealised_pnl": 211.2,
        "reason": "volume-backed expansion off VWAP",
    }
    row.update(overrides)
    return row


def test_the_agent_can_read_its_own_orders_and_what_they_are_worth() -> None:
    book = FakeBook([an_order()])
    toolbox, _ = box(book=book)

    result = toolbox.call("orders", {})

    assert result.ok is True
    body = payload(result)
    assert body["count"] == 1
    row = body["orders"][0]
    assert row["symbol"] == "DRREDDY"
    assert row["status"] == "open"
    # The live price and the mark-to-market travel with it, so the model is not
    # deciding blind about something it already holds.
    assert row["last_price"] == 1250.1
    assert row["unrealised_pnl"] == 211.2


def test_finished_orders_come_back_with_how_they_ended() -> None:
    book = FakeBook(
        [
            an_order(
                order_id="ord_2",
                status="closed",
                exit_reason="target",
                exit_price=1258.33,
                pnl={"gross": 474.24, "r_multiple": 1.56, "outcome": "win"},
            )
        ]
    )
    toolbox, _ = box(book=book)

    row = payload(toolbox.call("orders", {"status": "closed"}))["orders"][0]

    assert row["exit_reason"] == "target"
    assert row["pnl"]["outcome"] == "win"
    assert book.calls[0]["status"] == "closed"


def test_the_book_cannot_be_pointed_at_another_agent() -> None:
    """The id comes from the definition, never from what the model asked for."""

    book = FakeBook()
    toolbox, _ = box(book=book)

    toolbox.call("orders", {"agent_id": "someone-else"})

    assert book.calls[0]["agent_id"] == "alpha"


def test_the_limit_is_bounded_rather_than_obeyed() -> None:
    book = FakeBook()
    toolbox, _ = box(book=book)

    toolbox.call("orders", {"limit": 5000})
    toolbox.call("orders", {"limit": 0})

    assert book.calls[0]["limit"] == 25
    assert book.calls[1]["limit"] == 1


def test_an_unknown_order_status_is_refused_with_the_vocabulary() -> None:
    toolbox, _ = box(book=FakeBook())

    result = toolbox.call("orders", {"status": "winning"})

    assert result.ok is False
    assert "open" in result.text and "closed" in result.text


def test_a_limit_that_is_not_a_number_is_refused() -> None:
    toolbox, _ = box(book=FakeBook())

    result = toolbox.call("orders", {"limit": "lots"})

    assert result.ok is False
    assert "whole number" in result.text


def test_without_a_book_the_tool_says_so_rather_than_guessing() -> None:
    toolbox, _ = box()

    result = toolbox.call("orders", {})

    assert result.ok is False
    assert "no order book is configured" in result.text


def test_a_book_that_cannot_be_reached_is_reported_and_not_raised() -> None:
    toolbox, _ = box(book=FakeBook(error="Module 3 is unreachable"))

    result = toolbox.call("orders", {})

    assert result.ok is False
    assert "unreachable" in result.text


def test_the_order_book_is_offered_to_the_model() -> None:
    from module2.tools import tool_schemas

    names = [tool["function"]["name"] for tool in tool_schemas(definition())]

    assert "orders" in names


# -- the notebook ------------------------------------------------------------ #


def notes_box(tmp_path: Path) -> ToolBox:
    from module2.store import AgentStore

    store = AgentStore(tmp_path / "agents")
    store.agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    return ToolBox(
        market=FakeMarket(),  # type: ignore[arg-type]
        definition=definition(),
        store=store,
        market_state={"is_open": True},
        clock=lambda: NOW,
    )


def test_the_model_can_write_read_and_delete_its_own_note(tmp_path: Path) -> None:
    toolbox = notes_box(tmp_path)

    written = toolbox.call(
        "notes", {"action": "write", "key": "orl", "text": "breaks failed 3 of 4"}
    )
    listed = toolbox.call("notes", {"action": "list"})
    deleted = toolbox.call("notes", {"action": "delete", "key": "orl"})

    assert written.ok is True
    assert payload(written)["key"] == "orl"
    assert payload(listed)["notes"]["orl"] == "breaks failed 3 of 4"
    assert payload(deleted)["count"] == 0
    assert payload(toolbox.call("notes", {"action": "list"}))["count"] == 0


def test_the_notebook_is_offered_to_the_model(tmp_path: Path) -> None:
    from module2.tools import tool_schemas

    names = [tool["function"]["name"] for tool in tool_schemas(definition())]

    assert "notes" in names


def test_a_notes_refusal_is_handed_back_with_its_reason(tmp_path: Path) -> None:
    toolbox = notes_box(tmp_path)

    bad_action = toolbox.call("notes", {"action": "remember"})
    no_key = toolbox.call("notes", {"action": "write", "text": "a lesson"})
    too_long = toolbox.call(
        "notes", {"action": "write", "key": "k", "text": "x" * 601}
    )

    assert bad_action.ok is False and "not a notes action" in bad_action.text
    assert no_key.ok is False and "needs a key" in no_key.text
    assert too_long.ok is False and "600" in too_long.text


def test_the_notebook_never_spends_the_looking_around_budget(tmp_path: Path) -> None:
    """The mandatory write must not fail because the model looked too much."""

    from module2.store import AgentStore

    store = AgentStore(tmp_path / "agents")
    store.agent_dir("alpha").mkdir(parents=True, exist_ok=True)
    toolbox = ToolBox(
        market=FakeMarket(),  # type: ignore[arg-type]
        definition=definition(max_tool_calls_per_cycle=1),
        store=store,
        market_state={"is_open": True},
        clock=lambda: NOW,
    )

    assert toolbox.call("market_state", {}).ok is True
    written = toolbox.call(
        "notes", {"action": "write", "key": "k", "text": "still allowed"}
    )
    assert written.ok is True
    with pytest.raises(ToolBudgetExceeded):
        toolbox.call("market_state", {})


def test_without_a_store_the_notebook_says_so_rather_than_guessing() -> None:
    toolbox, _ = box()

    result = toolbox.call("notes", {"action": "list"})

    assert result.ok is False
    assert "no notebook is configured" in result.text

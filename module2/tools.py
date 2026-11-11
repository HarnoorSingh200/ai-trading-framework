"""The tools a model may call, and the refusal it gets when it may not.

Every market tool is a thin wrapper over one of Module 1's endpoints, and the
``orders`` tool is a thin wrapper over Module 3's. None of them places an
order, none of them reads another agent's state, and the only one that can end
a cycle is ``submit_decision``. The one tool that is not a wrapper is
``notes``: the agent's own notebook, the reasoning it carries forward forever
(:mod:`module2.notes`).

Two things are enforced here rather than asked for politely in the prompt:

* **the timeframe.** Each tool takes an ``interval`` argument whose default is
  the agent's own, so the model never has to remember it - and a value outside
  the timeframes the operator allowed is refused without a request leaving the
  process.
* **the size.** ``submit_decision`` has no quantity argument at all. What it
  returns has been sized by :mod:`module2.decision`, and a refusal is handed
  back as a tool result so the model can correct itself in the same cycle.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from module2 import notes as notebook
from module2.decision import (
    REASON_WORD_LIMIT,
    DecisionError,
    check,
    proposal_from_args,
)
from module2.definition import AgentDefinition
from module2.notes import MAX_KEY_CHARS, MAX_KEYS, MAX_NOTE_CHARS, NoteError
from module2.store import AgentStore
from module2.timeutil import utc_now

#: A candle series is for reading a shape, not for re-deriving the market: the
#: screen and the bundle already summarise it.
MAX_CANDLE_LIMIT: Final[int] = 200

#: The states an order can be read in. The vocabulary belongs to Module 3; this
#: copy exists so a bad status is refused here rather than across the wire.
ORDER_STATUSES: Final[tuple[str, ...]] = ("open", "pending", "closed", "expired")
#: How much of its own book an agent sees by default, and the most it may ask
#: for. Its own reasons travel with each order, so a long list is a lot of
#: context for something the model usually needs one glance at.
DEFAULT_ORDER_LIMIT: Final[int] = 10
MAX_ORDER_LIMIT: Final[int] = 25


class ToolBudgetExceeded(RuntimeError):
    """A cycle asked for more tool calls than its definition allows."""


class MarketGateway(Protocol):
    """The slice of Module 1 a tool may reach. Narrow on purpose."""

    def fields(self, interval: str) -> dict[str, Any]: ...

    def screen(self, **body: Any) -> dict[str, Any]: ...

    def stock(self, symbol: str, interval: str) -> dict[str, Any]: ...

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]: ...

    def market_state(self) -> dict[str, Any]: ...


class OrderGateway(Protocol):
    """The slice of Module 3 a tool may reach: this agent's own orders."""

    def mine(
        self, agent_id: str, *, status: str | None = None, limit: int = 10
    ) -> dict[str, Any]: ...

    def summary(self, agent_id: str, *, day: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the model is told, and whether the cycle is over."""

    ok: bool
    text: str
    terminal: bool = False
    decision: dict[str, Any] | None = None
    #: How long the tool took, filled in by the worker that ran it. Kept here
    #: rather than timed inside each tool so one clock measures every tool.
    ms: float = 0.0


def _function(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _interval_property(definition: AgentDefinition) -> dict[str, Any]:
    allowed = sorted(definition.allowed_intervals)
    return {
        "type": "string",
        "enum": allowed,
        "default": definition.timeframe,
        "description": (
            "Candle size to read. Defaults to this agent's own timeframe "
            f"({definition.timeframe}); it may also read {', '.join(allowed)}."
        ),
    }


def tool_schemas(definition: AgentDefinition) -> tuple[dict[str, Any], ...]:
    """The tool list offered to the model, in a stable order."""

    interval = _interval_property(definition)
    return (
        _function(
            "market_state",
            "The exchange session, whether the feed is live, and how fresh the "
            "history is. Call it if you need to know whether the market is open.",
            {"type": "object", "properties": {}},
        ),
        _function(
            "fields",
            "Every field a screen may filter on, its unit and the operators it "
            "takes. Call it before your first screen if you are unsure of a name.",
            {"type": "object", "properties": {"interval": interval}},
        ),
        _function(
            "screen",
            "Filter the whole universe on one timeframe and rank the matches. "
            "Every row describes the last closed candle, so the fields you compare "
            "are from the same bar. Use this to build your shortlist.",
            {
                "type": "object",
                "properties": {
                    "where": {
                        "type": "array",
                        "description": (
                            "Conditions, each {field, op, value}. Operators are "
                            "< <= > >= == !=. A null never matches."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "op": {"type": "string"},
                                "value": {"type": ["number", "string", "boolean"]},
                            },
                            "required": ["field", "op", "value"],
                        },
                    },
                    "match": {"type": "string", "enum": ["all", "any"], "default": "all"},
                    "rank_by": {
                        "type": "string",
                        "description": "numeric field to rank on",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 25,
                        "default": 5,
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "fields to return per row; defaults to the ones you "
                            "filtered on"
                        ),
                    },
                    "interval": interval,
                },
                "required": ["where"],
            },
        ),
        _function(
            "stock",
            "Everything about one stock on one timeframe: its finished candle, "
            "every indicator value on that candle, the previous session's levels "
            "and the day's own numbers. Use it on two or three names.",
            {
                "type": "object",
                "properties": {"symbol": {"type": "string"}, "interval": interval},
                "required": ["symbol"],
            },
        ),
        _function(
            "candles",
            "A series of recent candles for one stock, for reading a shape.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_CANDLE_LIMIT,
                        "default": 60,
                    },
                    "interval": interval,
                },
                "required": ["symbol"],
            },
        ),
        _function(
            "orders",
            "Your own orders, as the order book is holding them: what you are in "
            "right now, and how the trades you have already finished turned out. "
            "Only yours - never another agent's. Call it before you decide, so a "
            "trade you already hold does not get opened a second time.",
            {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(ORDER_STATUSES),
                        "description": (
                            "leave it out for every order, or ask for one state: "
                            "open is what you hold now, closed is how finished "
                            "trades ended"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_ORDER_LIMIT,
                        "default": DEFAULT_ORDER_LIMIT,
                    },
                },
            },
        ),
        _function(
            "notes",
            "Your own notebook, carried forward across every cycle, restart and "
            "session - the only memory you keep. Use action='list' to read it, "
            "'write' to replace one key, 'append' to extend it, 'delete' to drop "
            "a lesson that is no longer true. Keep one lesson per key and make it "
            "evidence-shaped: the condition, what you did, how it turned out. "
            "This notebook is shown to you at the start of every cycle and read "
            f"by the operator, and it is bounded ({MAX_KEYS} keys at most) - "
            "delete what stopped being true rather than letting it crowd out "
            "the rest. Updating it after your decisions is mandatory, before "
            "you sleep.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "write", "append", "delete"],
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "one lowercase key naming the lesson, up to "
                            f"{MAX_KEY_CHARS} characters; needed by write, "
                            "append and delete"
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "the lesson, up to "
                            f"{MAX_NOTE_CHARS} characters; needed by write and "
                            "append"
                        ),
                    },
                },
                "required": ["action"],
            },
        ),
        _function(
            "submit_decision",
            "Your answer, and the only one that is read. Call it exactly once, "
            "when you are done looking: either an order (buy or sell) with a "
            "symbol, a target, a stop and a holding time, or an explicit ignore. "
            "Do not give a quantity - the framework sizes the position from your "
            "stop and the agent's limits.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["buy", "sell", "ignore"]},
                    "symbol": {
                        "type": "string",
                        "description": "required for a buy or a sell",
                    },
                    "target": {
                        "type": "number",
                        "description": "price to take profit at, required for an order",
                    },
                    "stop_loss": {
                        "type": "number",
                        "description": "price to cut the loss at, required for an order",
                    },
                    "max_hold_minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "exit if neither target nor stop is hit by then"
                        ),
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {
                        "type": "string",
                        "description": (
                            "why this trade, one or two sentences and at most "
                            f"{REASON_WORD_LIMIT} words, naming the fields that "
                            "convinced you"
                        ),
                    },
                },
                "required": ["action", "reason"],
            },
        ),
    )


class ToolBox:
    """One cycle's tools: the definition, the budget and the market client."""

    def __init__(
        self,
        *,
        market: MarketGateway,
        definition: AgentDefinition,
        book: OrderGateway | None = None,
        store: AgentStore | None = None,
        market_state: Mapping[str, Any] | None = None,
        clock: Any = utc_now,
        stale_price_seconds: float = 120.0,
        close_at: datetime | None = None,
        bar_close_at: datetime | None = None,
    ) -> None:
        self._market = market
        self._definition = definition
        #: Where this agent's own orders can be read. Absent only when nothing
        #: has been configured to read them from, and the tool says so.
        self._book = book
        #: Where this agent's notebook lives. Absent in tests that only exercise
        #: the market tools, and the notes tool says so rather than failing.
        self._store = store
        self._market_state = dict(market_state or {})
        self._clock = clock
        self._stale_price_seconds = stale_price_seconds
        #: The framework close, and the candle being answered: the holding time
        #: is measured from the candle, so the last order of the day can use
        #: every minute it is actually entitled to.
        self._close_at = close_at
        self._bar_close_at = bar_close_at
        #: One bundle per symbol and timeframe is enough for one cycle: the
        #: model looked at it a moment ago, and it is the same candle.
        self._bundles: dict[tuple[str, str], Mapping[str, Any]] = {}
        #: A round's tool calls run in parallel, so the budget and the bundle
        #: cache are shared state and have to be treated as such.
        self._lock = threading.RLock()
        self.calls = 0

    @property
    def definition(self) -> AgentDefinition:
        return self._definition

    def call(self, name: str, arguments: Mapping[str, Any] | None) -> ToolResult:
        """Run one tool call, or explain why it will not be run."""

        args = dict(arguments or {})
        if name == "submit_decision":
            # The terminal tool is never rate-limited: refusing the answer would
            # leave a cycle with nothing to record at all.
            return self._submit(args)
        if name == "notes":
            # Never rate-limited either: the notebook update is mandatory at the
            # end of a cycle, and it must not fail because the budget for
            # looking around ran out.
            return self._notes(args)
        with self._lock:
            # Reserved before the work rather than after it: two calls in one
            # round would otherwise both pass the same check and spend a slot
            # between them that neither counted.
            if self.calls >= self._definition.max_tool_calls_per_cycle:
                raise ToolBudgetExceeded(
                    f"tool call budget exhausted ({self.calls} of "
                    f"{self._definition.max_tool_calls_per_cycle})"
                )
            self.calls += 1
        interval, refusal = self._resolve_interval(args)
        if refusal is not None:
            return refusal
        try:
            if name == "fields":
                payload = self._market.fields(interval)
            elif name == "screen":
                body = {key: value for key, value in args.items() if key != "interval"}
                body["interval"] = interval
                payload = self._market.screen(**body)
            elif name == "stock":
                symbol = str(args.get("symbol") or "").strip().upper()
                if not symbol:
                    return ToolResult(ok=False, text="refused: stock needs a symbol")
                payload = self._stock(symbol, interval)
            elif name == "candles":
                symbol = str(args.get("symbol") or "").strip().upper()
                if not symbol:
                    return ToolResult(ok=False, text="refused: candles needs a symbol")
                limit = int(args.get("limit") or 60)
                payload = self._market.candles(
                    symbol, interval, max(1, min(limit, MAX_CANDLE_LIMIT))
                )
            elif name == "market_state":
                payload = self._market.market_state()
            elif name == "orders":
                # Either this agent's orders, or the reason it cannot see them.
                fetched = self._orders(args)
                if isinstance(fetched, ToolResult):
                    return fetched
                payload = fetched
            else:
                return ToolResult(
                    ok=False,
                    text=(
                        f"refused: {name!r} is not a tool; this agent may call "
                        "market_state, fields, screen, stock, candles, orders, "
                        "notes and submit_decision"
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - the model is told, not crashed
            return ToolResult(
                ok=False, text=f"the tool failed: {type(exc).__name__}: {exc}"[:400]
            )
        return ToolResult(ok=True, text=json.dumps(payload, default=str, separators=(",", ":")))

    def _submit(self, args: Mapping[str, Any]) -> ToolResult:
        try:
            proposal = proposal_from_args(args)
        except DecisionError as exc:
            return ToolResult(ok=False, text=f"refused: {exc}")
        bundle: Mapping[str, Any] | None = None
        if proposal.action != "ignore" and proposal.symbol is not None:
            try:
                bundle = self._stock(proposal.symbol, self._definition.timeframe)
            except Exception as exc:  # noqa: BLE001 - reported like any other refusal
                return ToolResult(
                    ok=False,
                    text=(
                        f"refused: {proposal.symbol} could not be read "
                        f"({type(exc).__name__}: {exc})"
                    )[:400],
                )
        checked = check(
            proposal,
            definition=self._definition,
            bundle=bundle,
            now=self._clock(),
            session_open=bool(self._market_state.get("is_open")),
            stale_price_seconds=self._stale_price_seconds,
            close_at=self._close_at,
            bar_close_at=self._bar_close_at,
        )
        if not checked.ok:
            return ToolResult(ok=False, text="refused:\n- " + "\n- ".join(checked.messages))
        self.terminal = True
        return ToolResult(
            ok=True,
            text=json.dumps(
                {
                    "accepted": True,
                    "action": proposal.action,
                    "quantity": checked.quantity,
                },
                separators=(",", ":"),
            ),
            terminal=True,
            decision=_order_record(proposal, checked, bundle),
        )

    def _resolve_interval(self, args: Mapping[str, Any]) -> tuple[str, ToolResult | None]:
        raw = args.get("interval")
        value = str(raw).strip() if raw else self._definition.timeframe
        if value not in self._definition.allowed_intervals:
            allowed = ", ".join(sorted(self._definition.allowed_intervals))
            return value, ToolResult(
                ok=False,
                text=(
                    f"refused: {value} is not a timeframe this agent may read; it "
                    f"decides on {self._definition.timeframe} and may also read {allowed}"
                ),
            )
        return value, None

    def _orders(self, args: Mapping[str, Any]) -> dict[str, Any] | ToolResult:
        """This agent's own orders, or a refusal that says why it cannot have them.

        The agent id comes from the definition and never from the arguments: a
        model that could name the agent would be a model that could read another
        agent's book.
        """

        book = self._book
        if book is None:
            return ToolResult(
                ok=False,
                text=(
                    "refused: no order book is configured for this agent, so it "
                    "cannot see what it already holds"
                ),
            )
        raw_status = args.get("status")
        status = str(raw_status).strip().lower() if raw_status else ""
        if status and status not in ORDER_STATUSES:
            return ToolResult(
                ok=False,
                text=(
                    f"refused: {status!r} is not an order status; it is one of "
                    f"{', '.join(ORDER_STATUSES)}, or leave it out for all of them"
                ),
            )
        raw_limit = args.get("limit")
        if raw_limit is None or raw_limit == "":
            limit = DEFAULT_ORDER_LIMIT
        else:
            try:
                limit = int(str(raw_limit).strip())
            except ValueError:
                return ToolResult(
                    ok=False,
                    text=f"refused: limit must be a whole number, not {raw_limit!r}",
                )
        try:
            payload = book.mine(
                self._definition.agent_id,
                status=status or None,
                limit=max(1, min(limit, MAX_ORDER_LIMIT)),
            )
        except Exception as exc:  # noqa: BLE001 - the model is told, not crashed
            return ToolResult(
                ok=False, text=f"the order book could not be read: {str(exc)[:300]}"
            )
        # Module 3's own shape, passed through rather than re-cut here: two
        # descriptions of one order is how the two modules start to disagree
        # about what an order is.
        return {
            "count": payload.get("count", 0),
            "orders": payload.get("orders") or [],
        }

    def _notes(self, args: Mapping[str, Any]) -> ToolResult:
        """The agent's own notebook: read it, or change one key of it.

        The agent id comes from the definition and never from the arguments -
        the same rule as ``orders``. A model that could name the agent could
        read, or overwrite, another agent's reasoning.
        """

        store = self._store
        if store is None:
            return ToolResult(
                ok=False,
                text=(
                    "refused: no notebook is configured for this agent, so it "
                    "cannot carry notes forward"
                ),
            )
        action = str(args.get("action") or "").strip().lower()
        agent_id = self._definition.agent_id
        if action == "list":
            held = notebook.load(store, agent_id)
            return ToolResult(
                ok=True,
                text=json.dumps(
                    {
                        "action": "list",
                        "count": len(held),
                        "capacity": MAX_KEYS,
                        "notes": {
                            key: str(value.get("text") or "")
                            for key, value in held.items()
                        },
                    },
                    default=str,
                    separators=(",", ":"),
                ),
            )
        if action not in {"write", "append", "delete"}:
            return ToolResult(
                ok=False,
                text=(
                    f"refused: {action!r} is not a notes action; it is list, "
                    "write, append or delete"
                ),
            )
        try:
            outcome = notebook.apply(
                store,
                agent_id,
                action=action,
                key=str(args.get("key") or ""),
                text=str(args.get("text") or ""),
            )
        except NoteError as exc:
            return ToolResult(ok=False, text=f"refused: {exc}")
        return ToolResult(
            ok=True,
            text=json.dumps(outcome, default=str, separators=(",", ":")),
        )

    def _stock(self, symbol: str, interval: str) -> Mapping[str, Any]:
        """One stock's bundle, fetched once per cycle and then reused."""

        key = (symbol, interval)
        with self._lock:
            held = self._bundles.get(key)
        if held is not None:
            return held
        fetched = self._market.stock(symbol, interval)
        with self._lock:
            return self._bundles.setdefault(key, fetched)


def _order_record(
    proposal: Any, checked: Any, bundle: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The order as it will be shown, stored and checked afterwards."""

    bar = {} if bundle is None else (bundle.get("bar") or {})
    return {
        **proposal.describe(),
        "quantity": checked.quantity,
        "entry_price": None if checked.entry_price is None else float(checked.entry_price),
        "price_source": checked.price_source,
        "bar_open_time": bar.get("open_time"),
        "indicator_bar_open_time": (
            None if bundle is None else bundle.get("indicator_bar_open_time")
        ),
        "sizing": checked.sizing,
    }

"""One agent's loop, in its own process.

The shape of an agent's life: sleep, wake when a candle of its own timeframe
closes, look, answer, sleep. Everything it does is written down as it happens -
every tool call with its arguments, every result, the model's own words, the
decision - because the point of a log here is to be readable while the agent is
still working, not only afterwards.

Two rules hold the loop together:

* **one candle, one decision.** A bar that has been attempted is remembered on
  disk, so a restart in the middle of a bar cannot produce the same decision
  twice.
* **nothing is decided on a candle nobody can act on.** A bar that closes with
  the session is skipped without spending a model call on it, because the
  answer could only ever be "the market is closed".
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final
from uuid import uuid4

from common.telegram import Notifier, NullNotifier, escape
from module2 import notes as notebook
from module2.config import Settings
from module2.definition import AgentDefinition
from module2.llm import AsyncChatClient, ChatClient, Reply, as_async_client
from module2.market import MarketError
from module2.orders import DecisionSink
from module2.prompt import PROMPT_VERSION, build_messages, tool_schemas
from module2.schedule import (
    DEFAULT_CLOSE,
    DEFAULT_OPEN,
    next_decision_boundary,
    parse_clock,
    timeframe_minutes,
    wait_seconds,
)
from module2.store import AgentStore
from module2.timeutil import IST, format_utc, ist_today, parse_timestamp, utc_now
from module2.tools import OrderGateway, ToolBox, ToolBudgetExceeded, ToolResult

logger = logging.getLogger("module2.worker")

#: How long to wait before the single retry of a failed model call.
RETRY_SECONDS: float = 5.0

#: How long to wait between asking whether the bar has finished being built.
#: Short, because the answer usually arrives in about a second.
READY_POLL_SECONDS: float = 0.4

#: How many tools may run at once. A round asks about two or three names, so
#: this is headroom rather than a limit anyone should reach.
MAX_PARALLEL_TOOLS: int = 8

#: How much of the model's own prose is kept. Reasoning traces can run long;
#: anything cut is flagged rather than silently shortened.
MODEL_TEXT_LIMIT: int = 20_000

#: Said before the final round of a cycle. A cycle that spends its whole budget
#: and answers nothing has produced no decision at all - and a deliberate "I
#: stand aside" is worth more than silence, because it is a decision somebody
#: can read and judge afterwards.
LAST_ROUND_NUDGE: str = (
    "That was your last round of looking. Answer now with submit_decision: "
    "an order with a symbol, target, stop and holding time for each name you "
    "want to trade, or action='ignore' with your reason. Do not call another "
    "tool unless it is notes - the notebook update is still owed once you have "
    "decided."
)

#: Said when a model answers in prose without having decided anything. Prose is
#: not an answer: the framework reads one thing, and this says so once.
NOT_A_DECISION: str = (
    "That was not a decision. Call submit_decision: an order with a symbol, "
    "target, stop and holding time for each name worth trading, or "
    "action='ignore' with a reason."
)

#: Said when the model has decided but is about to end the cycle without
#: touching its notebook. The update is mandatory, and a model that is never
#: reminded will forget it on the cycles that mattered most.
NOTES_NUDGE: str = (
    "Before you sleep: update your notebook with the notes tool - one write, "
    "append or delete that records what this candle taught you. This is "
    "mandatory and it is the only memory that reaches the next session."
)

#: The final cycle gets a stronger instruction because it is the checkpoint
#: the next trading day will inherit. The model still uses the same mandatory
#: notebook tool; there is no second end-of-day model call to fail at shutdown.
END_OF_DAY_NOTES_NUDGE: str = (
    "This is the final decision cycle of the trading day. Before you sleep, "
    "update your notebook with one durable whole-day lesson: keep the evidence "
    "that still holds, rewrite what changed, and delete what is no longer true. "
    "The framework checkpoints this notebook at the close and carries it into "
    "the next trading day, so tomorrow's first cycle starts from it."
)

#: How many times the notebook is asked for before the cycle ends without it.
#: Twice is a reminder; a third would be the framework arguing with a model
#: that has stopped listening, at the cost of a model call each time.
MAX_NOTES_NUDGES: Final[int] = 2

#: The actions that become an order somewhere. A decision to stand aside is
#: recorded here and goes no further: there is nothing an order book could hold,
#: and sending it would only invite the log to say an order was handed over when
#: none was ever taken.
ORDER_ACTIONS: Final[frozenset[str]] = frozenset({"buy", "sell"})


@dataclass(slots=True)
class CycleOutcome:
    """What one wake-up produced.

    A cycle may produce more than one answer. An agent that shortlists three
    names and likes two of them places two orders in the same round, and the
    ledger holds two decisions sharing one cycle - so this carries a tuple, and
    ``decision`` is the first of them for callers that only want one.
    """

    status: str
    cycle_id: str | None = None
    decisions: tuple[dict[str, Any], ...] = ()
    rounds: int = 0
    tool_calls: int = 0
    #: Whether this cycle touched the notebook. The update is mandatory, so a
    #: cycle that decided and did not write is worth being able to count.
    notes_kept: bool = False
    error: str | None = None

    @property
    def decision(self) -> dict[str, Any] | None:
        return self.decisions[0] if self.decisions else None


class AgentWorker:
    """Runs one agent. One instance per process, one process per agent."""

    def __init__(
        self,
        definition: AgentDefinition,
        *,
        store: AgentStore,
        market: Any,
        client: ChatClient | AsyncChatClient,
        settings: Settings,
        sink: DecisionSink | None = None,
        book: OrderGateway | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.definition = definition
        self._store = store
        self._market = market
        #: One shape for the loop to await, whichever client it was given. A
        #: synchronous test client is run in a worker thread; the Responses
        #: client is native async and is used as it is.
        self._client = as_async_client(client)
        self._settings = settings
        #: Where a decided order is handed on, when there is anywhere to hand it.
        #: This module never reaches the broker, and neither does that one.
        self._sink = sink
        #: Where this agent can read its own orders back. The same service the
        #: sink writes to, read from the other direction.
        self._book = book
        #: Where the operator hears about orders and failures. Silent unless
        #: a bot is configured, and never allowed to fail a cycle.
        self._notifier = notifier or NullNotifier()
        self._clock = clock
        self._sleep = sleeper
        #: Operator policy, not exchange hours: when this agent may decide.
        self._window = settings.window
        self._settle = max(0.0, float(settings.candle_settle_seconds))
        self._stop = threading.Event()
        #: Set while run() is going, so a long wait can be cut short by a stop.
        self._halt = threading.Event()
        #: Serialises the activity log across a round's parallel tool calls.
        self._log_lock = threading.RLock()

    # -- one cycle ----------------------------------------------------------- #

    def cycle(
        self,
        *,
        bar_open_time: str,
        market_state: Mapping[str, Any] | None = None,
        note: str | None = None,
        bar_close_at: datetime | None = None,
        last_cycle: bool = False,
    ) -> CycleOutcome:
        """Run one cycle and wait for it.

        The work itself is async - a model call and a round of tools are both
        awaited - but a cycle is one unit of work, so this synchronous door is
        what a caller outside the loop uses (the tests included).
        """

        return asyncio.run(
            self._cycle(
                bar_open_time=bar_open_time,
                market_state=market_state,
                note=note,
                bar_close_at=bar_close_at,
                last_cycle=last_cycle,
            )
        )

    async def _cycle(
        self,
        *,
        bar_open_time: str,
        market_state: Mapping[str, Any] | None = None,
        note: str | None = None,
        bar_close_at: datetime | None = None,
        last_cycle: bool = False,
    ) -> CycleOutcome:
        """Look at one closed candle and answer, as often as it warrants.

        A cycle may answer more than once: an agent that shortlists three names
        and likes two of them places two orders, in the same round, and the
        ledger holds two decisions sharing one cycle. What the framework needs
        is not "one candle, one order" but "one candle, one pass, and the pass
        is written down".
        """

        started_monotonic = time.monotonic()
        cycle_id = f"cyc_{uuid4().hex[:12]}"
        state = dict(market_state or {})
        self._activity(
            {
                "type": "cycle_started",
                "cycle_id": cycle_id,
                "bar_open_time": bar_open_time,
                "market_open": bool(state.get("is_open")),
                "timeframe": self.definition.timeframe,
            }
        )
        toolbox = ToolBox(
            market=self._market,
            definition=self.definition,
            book=self._book,
            store=self._store,
            market_state=state,
            clock=self._clock,
            close_at=self._window.closes_at(self._clock()),
            bar_close_at=bar_close_at,
        )
        messages: list[dict[str, Any]] = build_messages(
            self.definition,
            bar_open_time=bar_open_time,
            now=self._clock(),
            book=self._book_line(),
            notes=notebook.digest(
                self._store,
                self.definition.agent_id,
                day=self._clock().astimezone(IST).date().isoformat(),
            ),
            note=note,
            window=self._window,
            bar_close_at=bar_close_at,
            end_of_day=last_cycle,
        )
        schemas = tool_schemas(self.definition)
        decisions: list[dict[str, Any]] = []
        noted = False
        note_nudges = 0
        rounds = 0
        while rounds < self.definition.max_tool_rounds:
            rounds += 1
            if rounds == self.definition.max_tool_rounds:
                messages.append({"role": "user", "content": LAST_ROUND_NUDGE})
            reply = await self._ask(messages, schemas, cycle_id=cycle_id)
            if reply is None:
                outcome = self._outcome(
                    # Orders already placed this cycle stand whatever happens
                    # next: the model failing afterwards does not unmake them.
                    "decided" if decisions else "failed",
                    decisions=decisions,
                    cycle_id=cycle_id,
                    rounds=rounds,
                    tool_calls=toolbox.calls,
                    error="the model could not be reached",
                )
                self._record_cycle_finished(
                    outcome,
                    cycle_id=cycle_id,
                    started_monotonic=started_monotonic,
                    rounds=rounds,
                    bar_open_time=bar_open_time,
                    market_open=bool(state.get("is_open")),
                    last_cycle=last_cycle,
                )
                if last_cycle:
                    self._checkpoint_session(outcome, bar_close_at=bar_close_at)
                return outcome
            self._log_text(reply, cycle_id=cycle_id)
            if not reply.tool_calls:
                if decisions:
                    if noted:
                        # It has answered, updated its notebook, and stopped
                        # asking for anything: the cycle is complete.
                        break
                    if note_nudges >= MAX_NOTES_NUDGES:
                        # It was reminded and still would not write. The
                        # decision stands; the silence is recorded below.
                        break
                    # It has decided but not yet done the one thing that
                    # survives the cycle. Remind it, at the cost of one round.
                    note_nudges += 1
                    messages.append(_assistant_text_message(reply))
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                END_OF_DAY_NOTES_NUDGE
                                if last_cycle
                                else NOTES_NUDGE
                            ),
                        }
                    )
                    continue
                # Prose alone is not an answer. Say so once, and keep the prose
                # in the log so it is visible what the model thought.
                messages.append(_assistant_text_message(reply))
                messages.append({"role": "user", "content": NOT_A_DECISION})
                continue
            messages.append(_assistant_message(reply))
            try:
                results = await self._run_calls(toolbox, reply.tool_calls, cycle_id=cycle_id)
            except ToolBudgetExceeded as exc:
                self._activity(
                    {"type": "budget_exhausted", "cycle_id": cycle_id, "message": str(exc)}
                )
                self._notifier.notify(
                    f"⚠️ <b>{escape(self.definition.agent_id)}</b> ran out of tool "
                    f"budget on candle {escape(bar_open_time)}: {escape(str(exc))}",
                    key=f"module2.{self.definition.agent_id}.budget",
                    dedupe_seconds=300.0,
                )
                break
            answered: list[dict[str, Any]] = []
            notes_round = 0
            for call, result in zip(reply.tool_calls, results, strict=True):
                messages.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": result.text}
                )
                if call.name == "notes" and result.ok:
                    notes_round += 1
                    action = str((call.arguments or {}).get("action") or "").lower()
                    if action in {"write", "append", "delete"}:
                        noted = True
                        self._activity(
                            {
                                "type": "notes_updated",
                                "cycle_id": cycle_id,
                                "action": action,
                                "key": (call.arguments or {}).get("key"),
                            }
                        )
                if result.terminal and result.decision is not None:
                    record = self._record_decision(
                        result.decision,
                        cycle_id=cycle_id,
                        bar_open_time=bar_open_time,
                    )
                    answered.append(record)
                    if str(record.get("action") or "").lower() in ORDER_ACTIONS:
                        self._notify_order(record)
            if answered:
                # Every order from this round goes to the book together, once
                # the round's decisions are all in hand. Only the orders: a
                # decision to stand aside is not something an order book can
                # hold, and logging one as handed over would send an operator
                # looking for a position that was never taken.
                orders = [
                    record
                    for record in answered
                    if str(record.get("action") or "").lower() in ORDER_ACTIONS
                ]
                if orders:
                    await self._forward_all(orders)
                decisions.extend(answered)
                if len(answered) == len(reply.tool_calls):
                    # The model answered and asked for nothing else in the same
                    # breath: it is done. Calling it again to be told so would
                    # be a second model call for a cycle that has already
                    # decided - and a model that wants several orders makes
                    # several submit_decision calls in one turn.
                    if noted:
                        break
                    if note_nudges >= MAX_NOTES_NUDGES:
                        break
                    # Decided, and the notebook is still owed. Say so now rather
                    # than spending a round discovering that the model is done.
                    note_nudges += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                END_OF_DAY_NOTES_NUDGE
                                if last_cycle
                                else NOTES_NUDGE
                            ),
                        }
                    )
                    continue
            if (
                decisions
                and noted
                and len(answered) + notes_round == len(reply.tool_calls)
            ):
                # The whole round was the decision and the notebook write, in
                # either order: nothing is owed, so the cycle ends here rather
                # than paying for one more model call to be told it is done.
                break
        if not decisions:
            self._activity(
                {
                    "type": "cycle_no_decision",
                    "cycle_id": cycle_id,
                    "reason": f"no decision within {self.definition.max_tool_rounds} rounds",
                }
            )
        if decisions and not noted:
            # The decision stands - the framework does not discard a call
            # because a note is missing - but the operator can see that this
            # cycle did not record anything for tomorrow.
            self._activity(
                {
                    "type": "notes_missing",
                    "cycle_id": cycle_id,
                    "rounds": rounds,
                    "message": "the cycle decided without updating its notebook",
                }
            )
        outcome = self._outcome(
            "decided" if decisions else "no_decision",
            decisions=decisions,
            cycle_id=cycle_id,
            rounds=rounds,
            tool_calls=toolbox.calls,
            notes_kept=noted,
        )
        self._record_cycle_finished(
            outcome,
            cycle_id=cycle_id,
            started_monotonic=started_monotonic,
            rounds=rounds,
            bar_open_time=bar_open_time,
            market_open=bool(state.get("is_open")),
            last_cycle=last_cycle,
        )
        if last_cycle:
            self._checkpoint_session(outcome, bar_close_at=bar_close_at)
        return outcome

    def step(
        self, *, state: dict[str, Any], halt: threading.Event | None = None
    ) -> CycleOutcome | None:
        """One wake-up, from outside the loop."""

        return asyncio.run(self._step(state=state, halt=halt))

    async def _step(
        self, *, state: dict[str, Any], halt: threading.Event | None = None
    ) -> CycleOutcome | None:
        """One wake-up: decide whether this candle needs a decision at all.

        Four questions before any model call, and three of them cost nothing:
        is the session open, has Module 1 finished building the bar, is it the
        candle we already did, and only then - is there anything to decide. A
        closed market and a candle already decided are not failures; they are
        the loop working.
        """

        stop = halt or self._halt
        session = self._session_state()
        if not session.get("is_open"):
            return None
        # The exchange may be open while the framework's own day is not: the
        # operator's window is what decides whether this candle may be answered.
        if not self._window.allows_decision(self._clock()):
            return None
        bar = self._await_bar(stop)
        if bar is None:
            return None
        stamp = bar.get("bar_open_time")
        if not stamp or state.get("last_attempted_bar") == stamp:
            return None
        bar_close_at = parse_timestamp(bar.get("bar_close_time"))
        following = next_decision_boundary(
            self._clock(),
            minutes=timeframe_minutes(self.definition.timeframe),
            window=self._window,
            session_open=parse_clock(session.get("session_open_ist"), DEFAULT_OPEN),
            session_close=parse_clock(session.get("session_close_ist"), DEFAULT_CLOSE),
            settle_seconds=0.0,
        )
        last_cycle = following is None
        outcome = await self._cycle(
            bar_open_time=str(stamp),
            market_state=session,
            note=_bar_note(bar),
            bar_close_at=bar_close_at,
            last_cycle=last_cycle,
        )
        state.update(
            {
                "last_attempted_bar": stamp,
                "last_status": outcome.status,
                "last_decision_count": len(outcome.decisions),
                "last_cycle_id": outcome.cycle_id,
                "last_run_at": format_utc(self._clock()),
            }
        )
        self._store.save_state(self.definition.agent_id, state)
        return outcome

    # -- the process --------------------------------------------------------- #

    def run(self, *, stop: threading.Event | None = None) -> None:
        """Run this agent for the day, in one event loop."""

        asyncio.run(self._run(stop=stop))

    async def _run(self, *, stop: threading.Event | None = None) -> None:
        """Wake on this agent's own clock, decide, sleep, repeat.

        The agent used to sleep on a stream of candle-closed announcements from
        Module 1, which put two delays in front of every decision that had
        nothing to do with the work: the announcement's own latency, and a fixed
        settle period sitting behind it. It knows its own timeframe and the
        session's hours, so it keeps its own clock instead - and is ready *at*
        the boundary rather than told about it *after*.
        """

        halt = stop or self._stop
        self._halt = halt
        self._activity(
            {
                "type": "agent_started",
                "timeframe": self.definition.timeframe,
                "model": self.definition.model,
                "window": self._window.describe(),
            }
        )
        self._notifier.notify(
            f"🤖 <b>{escape(self.definition.agent_id)}</b> started "
            f"({escape(self.definition.timeframe)} · {escape(self.definition.model)}). "
            f"Decisions {self._window.decision_open.strftime('%H:%M')}-"
            f"{self._window.decision_close.strftime('%H:%M')} IST, "
            f"flat by {self._window.close.strftime('%H:%M')}.",
            key=f"module2.{self.definition.agent_id}.started",
            dedupe_seconds=0.0,
        )
        if not self._wait_for_market(halt):
            self._activity(
                {
                    "type": "agent_failed",
                    "error": "Module 1 never became ready, so no candle could be read",
                }
            )
            self._notifier.notify(
                f"❌ <b>{escape(self.definition.agent_id)}</b> could not start: "
                "Module 1 never became ready.",
                key=f"module2.{self.definition.agent_id}.no_market",
                dedupe_seconds=0.0,
            )
            return
        state = self._store.load_state(self.definition.agent_id)
        # A fresh agent must not answer a candle that closed before it existed:
        # the moment for that decision has passed, and a 15-minute agent
        # restarted at 11:03 would otherwise book a trade on the 10:45 bar. It
        # waits for the next boundary of its own clock, exactly as it does
        # between cycles - but only while the day still holds a decision.
        now = self._clock()
        if now < self._window.last_decision_moment(now):
            self._sleep_for_next_wake(halt)
        finished_for_day = False
        while not halt.is_set():
            if not self._wait_for_window(halt):
                finished_for_day = True
                self._activity(
                    {
                        "type": "session_window_closed",
                        "window": self._window.describe(),
                    }
                )
                self._notifier.notify(
                    f"🌙 <b>{escape(self.definition.agent_id)}</b> finished for the "
                    f"day: the decision window ({self._window.decision_open.strftime('%H:%M')}-"
                    f"{self._window.decision_close.strftime('%H:%M')} IST) is over.",
                    key=f"module2.{self.definition.agent_id}.day_done",
                    dedupe_seconds=0.0,
                )
                break
            if halt.is_set():
                break
            try:
                await self._step(state=state, halt=halt)
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the agent
                self._activity(
                    {"type": "cycle_failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
                )
                self._notifier.notify(
                    f"⚠️ <b>{escape(self.definition.agent_id)}</b> cycle failed: "
                    f"{escape(f'{type(exc).__name__}: {exc}')}"[:300],
                    key=f"module2.{self.definition.agent_id}.cycle_failed",
                    dedupe_seconds=120.0,
                )
                halt.wait(2.0)
            if halt.is_set():
                break
            self._sleep_for_next_wake(halt)
        self._activity({"type": "agent_stopped"})
        if not finished_for_day:
            self._notifier.notify(
                f"🛑 <b>{escape(self.definition.agent_id)}</b> stopped.",
                key=f"module2.{self.definition.agent_id}.stopped",
                dedupe_seconds=0.0,
            )

    def stop(self) -> None:
        self._stop.set()

    # -- internals ----------------------------------------------------------- #

    async def _ask(
        self,
        messages: Sequence[Mapping[str, Any]],
        schemas: Sequence[Mapping[str, Any]],
        *,
        cycle_id: str,
    ) -> Reply | None:
        """One model call, retried exactly once. ``None`` means give up."""

        for attempt in (1, 2):
            reply = await self._client.acomplete(
                messages=messages,
                tools=schemas,
                temperature=self.definition.temperature,
                max_tokens=self.definition.max_tokens,
                timeout=self.definition.cycle_timeout_seconds,
            )
            if reply.ok:
                # How long the model took, and what it cost in tokens, written
                # where every other stage of the cycle already is: a latency
                # analysis that has to guess which part was slow is not one.
                usage = reply.usage if isinstance(reply.usage, Mapping) else {}
                self._activity(
                    {
                        "type": "model_reply",
                        "cycle_id": cycle_id,
                        "attempt": attempt,
                        "latency_ms": round(float(reply.latency_ms or 0.0), 1),
                        "tool_calls": len(reply.tool_calls),
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        # What the prompt cache saved and how much of the
                        # completion was thinking: the two numbers the bill is
                        # actually made of.
                        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
                        "prompt_cache_miss_tokens": usage.get(
                            "prompt_cache_miss_tokens"
                        ),
                        "reasoning_tokens": usage.get("reasoning_tokens"),
                    }
                )
                return reply
            self._activity(
                {
                    "type": "model_error",
                    "cycle_id": cycle_id,
                    "attempt": attempt,
                    "status": reply.status,
                    "error": reply.error,
                }
            )
            self._notifier.notify(
                f"⚠️ <b>{escape(self.definition.agent_id)}</b> model call failed "
                f"({escape(reply.status)}, attempt {attempt}): {escape(reply.error)}",
                key=f"module2.{self.definition.agent_id}.model_error",
                dedupe_seconds=300.0,
            )
            if attempt == 1:
                await asyncio.to_thread(self._sleep, RETRY_SECONDS)
        self._activity(
            {
                "type": "cycle_failed",
                "cycle_id": cycle_id,
                "error": "the model could not be reached",
            }
        )
        return None

    def _wait_for_window(self, halt: threading.Event) -> bool:
        """Wait for the decision window. ``False`` means the day is over.

        Before the open - and between the last decision and the next morning -
        the agent looks again every minute rather than trusting one reading of
        somebody else's clock for hours.
        """

        now = self._clock()
        if self._window.allows_decision(now):
            return True
        if now >= self._window.last_decision_moment(now):
            return False
        halt.wait(min(60.0, self._window.seconds_until_open(now)))
        return True

    def _sleep_for_next_wake(self, halt: threading.Event) -> None:
        """Wait until the next candle of this agent's own timeframe has closed.

        The wait is the boundary **plus a settle buffer**: Module 1 closes a
        minute when the first tick of the next one arrives, so the candle and
        the indicators computed from it exist a second or two after the clock
        turns - five seconds is the buffer the operator asked for, and it turns
        "should be ready by now" into a measurement.

        When the day has no bars left it waits for the next session rather than
        waking every candle to be told the window is shut.
        """

        now = self._clock()
        minutes = timeframe_minutes(self.definition.timeframe)
        session = self._session_state()
        opened = parse_clock(session.get("session_open_ist"), DEFAULT_OPEN)
        closed = parse_clock(session.get("session_close_ist"), DEFAULT_CLOSE)
        candidate = next_decision_boundary(
            now,
            minutes=minutes,
            window=self._window,
            session_open=opened,
            session_close=closed,
            settle_seconds=self._settle,
        )
        if candidate is None:
            reopened = parse_timestamp(session.get("next_open"))
            if reopened is not None:
                halt.wait(min(60.0, wait_seconds(now, reopened)))
                return
            halt.wait(60.0)
            return
        halt.wait(wait_seconds(now, candidate))

    def _await_bar(self, halt: threading.Event) -> dict[str, Any] | None:
        """The last closed bar, once Module 1 has finished building it.

        The loop used to sleep a flat 45 seconds for this because nobody could
        say how long it took. The book can say: it knows how many symbols have
        a finished bar for that bucket and how many are still short of minutes.
        So the agent wakes on the boundary, asks, and starts the moment the
        answer is yes - usually about a second later, and never for longer than
        the operator allows.
        """

        timeout = max(0.0, float(self._settings.bar_timeout_seconds))
        fraction = min(1.0, max(0.0, float(self._settings.bar_ready_fraction)))
        deadline = time.monotonic() + timeout
        waited = 0.0
        last: dict[str, Any] = {}
        while not halt.is_set():
            try:
                last = self._market.bar_status(self.definition.timeframe)
            except MarketError as exc:
                self._activity(
                    {
                        "type": "market_error",
                        "error": str(exc)[:300],
                        # A 404 here is not a fault, it is a version: this agent
                        # engine asks a question the running Module 1 does not
                        # answer yet. Say so, rather than looking idle.
                        "hint": (
                            "Module 1 does not publish /v1/market/bar-status, so "
                            "it is older than this agent engine and needs "
                            "restarting"
                            if exc.status_code == 404
                            else None
                        ),
                    }
                )
                self._notifier.notify(
                    f"⚠️ <b>{escape(self.definition.agent_id)}</b> cannot read the "
                    f"candle from Module 1: {escape(str(exc))}",
                    key=f"module2.{self.definition.agent_id}.market_error",
                    dedupe_seconds=300.0,
                )
                return None
            total = int(last.get("symbols") or 0)
            ready = int(last.get("ready") or 0)
            if total and ready >= fraction * total:
                return last
            if time.monotonic() >= deadline:
                # Decide anyway, and say how short the data was: a bar missing
                # minutes is marked as missing, and the model is told to treat
                # it as weaker evidence rather than to pretend it is whole.
                last["waited_seconds"] = round(waited, 1)
                last["timed_out"] = True
                return last
            halt.wait(READY_POLL_SECONDS)
            waited += READY_POLL_SECONDS
        return None

    def _book_line(self) -> str:
        """What this agent already holds, and how its day has gone.

        Both halves of the agent's own memory, fetched before the model is asked
        anything. Finding out used to cost a whole round - a model call, seconds
        - and it is one local request.

        Saying nothing would be worse than saying "unknown". An agent that
        cannot see its book opens the same trade twice, which one of them did;
        an agent that cannot see its results has no way to notice that a way of
        trading is not working.
        """

        book = self._book
        if book is None:
            return "You cannot read your own orders from here."
        try:
            # Two questions, because they have two different answers. What the
            # agent *holds* is not a thing that resets at midnight: an open
            # position is open whichever day it was taken on, and hiding one
            # would be how it ends up buying the same stock twice. What it has
            # *made* is a fact about today - a running total that never resets
            # tells an agent nothing about how it is reading this tape now.
            holding = book.summary(self.definition.agent_id)
            today = book.summary(
                self.definition.agent_id, day=ist_today().isoformat()
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self._activity(
                {
                    "type": "book_unreadable",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
            self._notifier.notify(
                f"⚠️ <b>{escape(self.definition.agent_id)}</b> cannot read its own "
                f"order book: {escape(f'{type(exc).__name__}: {exc}'[:200])}",
                key=f"module2.{self.definition.agent_id}.book",
                dedupe_seconds=300.0,
            )
            return "Your open positions could not be read; call orders before you decide."
        return _book_sentence(holding, today)

    def _outcome(
        self,
        status: str,
        *,
        decisions: Sequence[dict[str, Any]],
        cycle_id: str,
        rounds: int,
        tool_calls: int,
        notes_kept: bool = False,
        error: str | None = None,
    ) -> CycleOutcome:
        return CycleOutcome(
            status=status,
            cycle_id=cycle_id,
            decisions=tuple(decisions),
            rounds=rounds,
            tool_calls=tool_calls,
            notes_kept=notes_kept,
            error=error,
        )

    def _record_cycle_finished(
        self,
        outcome: CycleOutcome,
        *,
        cycle_id: str,
        started_monotonic: float,
        rounds: int,
        bar_open_time: str,
        market_open: bool,
        last_cycle: bool,
    ) -> None:
        """Record the wall-clock cost of the work, before the sleep after it.

        The process that did the work takes this measurement. It covers model
        calls, parallel tool rounds, order hand-off and notebook writes, and it
        deliberately excludes the wait for the next candle. That distinction is
        what makes a cycle duration useful rather than a measure of the gap
        between decisions.
        """

        self._activity(
            {
                "type": "cycle_finished",
                "cycle_id": cycle_id,
                "bar_open_time": bar_open_time,
                "market_open": market_open,
                "status": outcome.status,
                "duration_ms": round(
                    max(0.0, time.monotonic() - started_monotonic) * 1000.0,
                    1,
                ),
                "rounds": rounds,
                "tool_calls": outcome.tool_calls,
                "decision_count": len(outcome.decisions),
                "notes_kept": outcome.notes_kept,
                "last_cycle": last_cycle,
                "error": outcome.error,
            }
        )

    def _checkpoint_session(
        self, outcome: CycleOutcome, *, bar_close_at: datetime | None
    ) -> None:
        """Persist the final-cycle notebook as the next session's hand-off."""

        session_day = (
            bar_close_at.astimezone(IST).date().isoformat()
            if bar_close_at is not None
            else self._clock().astimezone(IST).date().isoformat()
        )
        checkpoint = notebook.checkpoint_session(
            self._store,
            self.definition.agent_id,
            day=session_day,
            cycle_id=outcome.cycle_id,
        )
        self._activity(
            {
                "type": "session_checkpointed",
                "cycle_id": outcome.cycle_id,
                **checkpoint,
            }
        )

    def _log_text(self, reply: Reply, *, cycle_id: str) -> None:
        if not reply.text:
            return
        self._activity(
            {
                "type": "model_text",
                "cycle_id": cycle_id,
                "text": reply.text[:MODEL_TEXT_LIMIT],
                # A cut is never silent: the feed can say the words continue
                # rather than implying that was all of them.
                "truncated": len(reply.text) > MODEL_TEXT_LIMIT,
                "full_length": len(reply.text),
            }
        )

    def _record_decision(
        self, decision: dict[str, Any], *, cycle_id: str, bar_open_time: str
    ) -> dict[str, Any]:
        record = self._stamp(decision, cycle_id=cycle_id, bar_open_time=bar_open_time)
        self._store.append_decision(self.definition.agent_id, record)
        self._activity({"type": "decision", "cycle_id": cycle_id, "decision": record})
        return record

    def _notify_order(self, record: dict[str, Any]) -> None:
        """Tell the operator an order was decided, in the terms a trade is read in.

        This is the decision, not the fill: the plan's entry is where the
        market was when the model looked, and Module 3 will say separately what
        price it was actually taken at.
        """

        side = str(record.get("action") or "").upper()
        icon = "🟢" if side == "BUY" else "🔴"
        sizing = record.get("sizing") or {}
        reward_risk = "n/a"
        try:
            entry = float(record["entry_price"])
            stop = float(record["stop_loss"])
            target = float(record["target"])
            risk = abs(entry - stop)
            reward_risk = f"{abs(target - entry) / risk:.2f}" if risk else "n/a"
        except (KeyError, TypeError, ValueError):
            pass
        lines = [
            f"{icon} <b>{escape(side)} {escape(record.get('symbol'))}</b> · "
            f"{escape(record.get('agent_id'))}",
            f"quantity <b>{escape(record.get('quantity'))}</b> · entry "
            f"{escape(record.get('entry_price'))} ({escape(record.get('price_source'))}) "
            f"· stop {escape(record.get('stop_loss'))} · target "
            f"{escape(record.get('target'))} · hold "
            f"{escape(record.get('max_hold_minutes'))}m · R:R {reward_risk}",
            f"risk {escape(sizing.get('money_at_risk'))} · notional "
            f"{escape(sizing.get('notional'))} · "
            f"{escape(record.get('timeframe'))} candle "
            f"{escape(record.get('bar_open_time'))}",
            f"reason: {escape(record.get('reason'))}",
        ]
        self._notifier.notify(
            "\n".join(lines),
            key=f"module2.order.{record.get('decision_id')}",
            dedupe_seconds=0.0,
        )

    async def _run_calls(
        self, toolbox: ToolBox, calls: Sequence[Any], *, cycle_id: str
    ) -> list[ToolResult]:
        """Run one round's tool calls together, in the order the model asked.

        The calls in a round do not depend on each other - a model that asks
        about three stocks in one turn wants all three - so running them one
        after another is paying three times over for one answer.

        The log still reads in order: every call is written before the round
        starts and every result as the round ends, rather than interleaved by
        whichever thread happened to finish first.
        """

        for call in calls:
            if call.name != "submit_decision":
                self._activity(
                    {
                        "type": "tool_call",
                        "cycle_id": cycle_id,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "call_id": call.call_id,
                    }
                )
        # A round is a fan-out: every call is independent. The semaphore is
        # the same headroom the thread pool used to provide - a model that asks
        # about twelve stocks in one turn must not open twelve connections to
        # Module 1 - and gather keeps the answers in the order they were asked.
        limit = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

        async def one(call: Any) -> ToolResult:
            async with limit:
                return await asyncio.to_thread(self._execute, toolbox, call)

        results = list(await asyncio.gather(*(one(call) for call in calls)))
        for call, result in zip(calls, results, strict=True):
            self._activity(
                {
                    "type": "tool_result",
                    "cycle_id": cycle_id,
                    "tool": call.name,
                    "ok": result.ok,
                    # How long this call took. Calls in one round run together,
                    # so these overlap rather than add up - which is exactly
                    # what a latency analysis has to be able to see.
                    "ms": round(float(result.ms), 1),
                    # Whole. This is the record of what the model was actually
                    # told, and a result cut at 1200 characters reads as a tool
                    # that is broken rather than a log that is short.
                    "text": result.text,
                }
            )
        return results

    def _execute(self, toolbox: ToolBox, call: Any) -> ToolResult:
        """One tool call, with its failure turned into an answer, timed."""

        started = time.monotonic()
        try:
            result = toolbox.call(call.name, call.arguments)
        except ToolBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - a tool bug is not fatal
            result = ToolResult(
                ok=False, text=f"the tool failed: {type(exc).__name__}: {exc}"[:400]
            )
        return replace(result, ms=round((time.monotonic() - started) * 1000, 1))

    def _wait_for_market(self, stop: threading.Event) -> bool:
        deadline = time.monotonic() + max(1.0, float(self._settings.ready_timeout_seconds))
        while not stop.is_set():
            try:
                self._market.status()
                return True
            except MarketError as exc:
                logger.info("%s: waiting for Module 1 (%s)", self.definition.agent_id, exc)
            if time.monotonic() >= deadline:
                return False
            stop.wait(2.0)
        return False

    def _session_state(self) -> dict[str, Any]:
        """What the exchange session is doing, asked of the module that owns it.

        Used when a wake-up arrives without it. An empty answer is left empty
        rather than filled in with a guess: a cycle that cannot tell whether the
        market is open will fail on its own a moment later, for a reason it can
        state, rather than here for one it cannot.
        """

        try:
            return self._market.market_state().get("session") or {}
        except MarketError as exc:
            self._activity({"type": "market_error", "error": str(exc)[:300]})
            self._notifier.notify(
                f"⚠️ <b>{escape(self.definition.agent_id)}</b> cannot read the "
                f"session from Module 1: {escape(str(exc))}",
                key=f"module2.{self.definition.agent_id}.session_read",
                dedupe_seconds=300.0,
            )
            return {}

    def _stamp(
        self, decision: dict[str, Any], *, cycle_id: str, bar_open_time: str
    ) -> dict[str, Any]:
        """Everything that makes a decision reproducible after the fact."""

        record = {
            **decision,
            "decision_id": f"dec_{uuid4().hex[:12]}",
            "agent_id": self.definition.agent_id,
            "cycle_id": cycle_id,
            # One candle can now carry several decisions, so the key names the
            # stock as well - otherwise two orders from one bar would share a
            # key and neither could be traced on its own.
            "decision_key": (
                f"{self.definition.agent_id}:{self.definition.timeframe}:"
                f"{bar_open_time}:{decision.get('symbol') or 'no-symbol'}"
            ),
            "timeframe": self.definition.timeframe,
            "model": self.definition.model,
            "prompt_version": PROMPT_VERSION,
            "definition_hash": self.definition.definition_hash(),
            "created_at": format_utc(self._clock()),
        }
        # The candle this decision was about. A decision to stand aside inspects
        # no stock, so there is no bundle to take the bar from - and without this
        # the one thing missing from the record would be the moment it was about.
        if not record.get("bar_open_time"):
            record["bar_open_time"] = bar_open_time
        return record

    def _activity(self, event: dict[str, Any]) -> None:
        # A round's tool calls run in parallel and each writes a line; one file
        # written by several threads at once is one file with several threads'
        # worth of half-written lines in it.
        lock = self._log_lock
        try:
            with lock:
                self._store.append_activity(self.definition.agent_id, event)
        except OSError as exc:  # pragma: no cover - a full disk must not kill the agent
            logger.warning("%s: activity not written: %s", self.definition.agent_id, exc)

    def _deliver(self, record: dict[str, Any]) -> tuple[bool, str | None]:
        """Hand one order over. ``(arrived, why not)``."""

        sink = self._sink
        if sink is None:
            return False, None
        try:
            delivered = bool(sink.deliver(record))
        except Exception as exc:  # noqa: BLE001 - a sink bug must not fail a cycle
            return False, f"{type(exc).__name__}: {exc}"[:300]
        return delivered, None if delivered else _sink_error(sink)

    async def _forward_all(self, records: Sequence[dict[str, Any]]) -> None:
        """Hand a round's orders over together, then say how each went.

        The records are already written by the time this runs, so a hand-over
        that fails is a delay rather than a loss - Module 3 reconciles against
        this module's decisions. It is still worth saying out loud, because an
        order that never reached the book is the one thing an operator cannot
        see from either side.

        The posts run at once because they are independent; the log lines are
        written afterwards, in the order the decisions were made, because the
        log is one file.
        """

        if self._sink is None:
            return
        if len(records) == 1:
            outcomes = [self._deliver(records[0])]
        else:
            outcomes = list(
                await asyncio.gather(
                    *(asyncio.to_thread(self._deliver, record) for record in records)
                )
            )
        for record, (delivered, error) in zip(records, outcomes, strict=True):
            self._activity(
                {
                    "type": "order_forwarded" if delivered else "order_forward_failed",
                    "cycle_id": record.get("cycle_id"),
                    "decision_id": record.get("decision_id"),
                    "error": error,
                }
            )
            if not delivered:
                self._notifier.notify(
                    f"⚠️ <b>{escape(self.definition.agent_id)}</b> could not hand "
                    f"<b>{escape(record.get('symbol'))}</b> to the order manager "
                    f"({escape(error) or 'no reason recorded'}); it is recorded and "
                    "Module 3 will reconcile it.",
                    key=f"module2.forward.{record.get('decision_id')}",
                    dedupe_seconds=0.0,
                )


def _sink_error(sink: Any) -> str | None:
    error = getattr(sink, "last_error", None)
    return None if error is None else str(error)[:300]


def _book_sentence(holding: Mapping[str, Any], today: Mapping[str, Any]) -> str:
    """The agent's own record in one paragraph.

    What it is holding *wherever it was taken*, then how *today* has gone. Both
    in the terms a trader would use out loud - sizes and levels for the
    positions, rupees and R for the results - because the point is to be read,
    not parsed.
    """

    positions = holding.get("open_positions") or []
    if positions:
        held = "; ".join(
            f"{row.get('symbol')} {row.get('side')} ×{row.get('quantity')} at "
            f"{row.get('entry_price')} (stop {row.get('stop_loss')}, "
            f"target {row.get('target')})"
            for row in positions
        )
        opening = f"You are already holding {len(positions)}: {held}."
    else:
        opening = "You are holding nothing right now."

    closed = int(today.get("closed") or 0)
    if not closed:
        return f"{opening} Nothing has closed today yet, so there is no result to judge."

    realised = float(today.get("realised") or 0.0)
    wins = int(today.get("wins") or 0)
    losses = int(today.get("losses") or 0)
    line = (
        f"Today you have closed {closed} {'trade' if closed == 1 else 'trades'} "
        f"for {realised:+.2f}, {wins} up and {losses} down"
    )
    average = today.get("average_r")
    if average is not None:
        line += f", averaging {float(average):+.2f}R"
    return f"{opening} {line}. Ask orders for the detail."


def _bar_note(bar: Mapping[str, Any]) -> str | None:
    """How complete the bar's data was, when it was not complete.

    Silent when everything landed: a line saying "all fifty are here" on every
    cycle is noise, and the model has the rows to read. It speaks only when the
    bar is short, because that is the moment the answer changes a decision.
    """

    total = int(bar.get("symbols") or 0)
    ready = int(bar.get("ready") or 0)
    if not total or ready >= total:
        return None
    waited = bar.get("waited_seconds")
    tail = (
        f"after waiting {waited}s, and their rows say so"
        if waited
        else "and their rows say so"
    )
    return (
        f"{ready} of {total} stocks have a finished bar for this candle; the rest "
        f"are short of minutes {tail}."
    )


def _assistant_message(reply: Reply) -> dict[str, Any]:
    """The assistant turn that asked for tools, in the shape a provider expects."""

    message: dict[str, Any] = {
        "role": "assistant",
        "content": reply.text or "",
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, default=str),
                },
            }
            for call in reply.tool_calls
        ],
    }
    _keep_provider_items(message, reply)
    return message


def _assistant_text_message(reply: Reply) -> dict[str, Any]:
    """An assistant turn that only wrote prose; provider items travel with it."""

    message: dict[str, Any] = {"role": "assistant", "content": reply.text or ""}
    _keep_provider_items(message, reply)
    return message


def _keep_provider_items(message: dict[str, Any], reply: Reply) -> None:
    """Keep the provider's own turn for the next call, without leaking it out.

    The Responses API wants its reasoning items and function calls replayed
    exactly as it produced them. They are attached to the assistant message
    under a private key that the Chat Completions transport strips; the worker
    itself never reads them, and they never reach the activity log.
    """

    if reply.provider_items:
        message["_provider_items"] = [dict(item) for item in reply.provider_items]

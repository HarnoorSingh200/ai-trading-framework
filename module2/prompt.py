"""What the model is told, in the order it is told it.

The system half is built from the definition alone, so it is byte-identical
between cycles and the provider's prompt cache can do its job - which is why
the volatile half (the clock, the candle that just closed) is a separate
message at the end rather than an interpolation at the top.

The operator's own system prompt is carried verbatim in a delimited block. It is
their instruction, not this module's, and paraphrasing it would be the one place
this framework silently edits what the operator asked for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from common.window import TradingWindow
from module2.definition import AgentDefinition
from module2.timeutil import format_ist
from module2.tools import tool_schemas

#: Bumped when the fixed half changes, so a decision can be traced to the
#: instructions that produced it. 1.1.0 gave the agent its own order book;
#: 1.2.0 has the worker hand it over, with the day's results, before asking;
#: 1.3.0 states the operator's trading window (09:30-14:30, flat by 14:50);
#: 1.4.0 gives it a notebook that survives every session, with the update
#: mandatory after its decisions and before it sleeps. 1.5.0 states the close
#: rules as one place: no new order after the decision window, and every open
#: position is flattened at the framework close. 1.6.0 puts a written limit on
#: the reason: fifty words, naming the fields.
PROMPT_VERSION: Final[str] = "1.6.0"

__all__ = [
    "PROMPT_VERSION",
    "build_messages",
    "system_message",
    "tool_schemas",
]


def system_message(
    definition: AgentDefinition, *, window: TradingWindow | None = None
) -> str:
    """The cacheable half: the frame, the rules and the operator's own words."""

    held = window or TradingWindow()
    context = (
        ", ".join(definition.context_timeframes)
        if definition.context_timeframes
        else "none"
    )
    shorting = "You may open a short as well as a long." if definition.allow_short else (
        "You trade long only: a short will be refused."
    )
    return f"""\
You are one trading agent in an intraday framework for the Indian market. You
make one decision at a time, on the candle that has just closed, and you are
finished once you have answered.

Your market
- You decide on the {definition.timeframe} timeframe. That is the candle you are
  woken for, and the only one whose close is a signal.
- You may also read: {context}. Anything else will be refused.
- Every candle number you are shown describes one finished candle. A value of
  null means there is not enough history behind it yet - it is not a zero, and
  it is not a signal.

Your day
- You are woken on each {definition.timeframe} bar close between
  {held.decision_open.strftime('%H:%M')} and {held.decision_close.strftime('%H:%M')} IST.
- No new order is accepted after the {held.decision_close.strftime('%H:%M')} decision
  window. The candle that closes at {held.decision_close.strftime('%H:%M')} is answered
  only in the framework's short grace period; do not plan around a later entry.
- The framework is flat by {held.close.strftime('%H:%M')} IST. Any position still open
  then is closed automatically by the order manager, and a pending order that never
  filled expires.
- The last candle of the day closes at {held.decision_close.strftime('%H:%M')} IST, and the
  framework goes flat at {held.close.strftime('%H:%M')} IST: an order taken on that candle may
  hold at most {held.last_order_minutes()} minutes, however much time the limit below allows.

Your money
- Capital {definition.capital:,.0f} at {definition.leverage:g}x leverage.
- Risk per trade {definition.risk_per_trade_pct:g}% of capital, and at most
  {definition.max_capital_per_trade_pct:g}% of buying power in one position.
- You do not choose a quantity. Give a target and a stop, and the framework
  sizes the position from the distance between them and the limits above.
- Maximum holding time {definition.max_hold_minutes} minutes, and no position
  may still be open after {held.close.strftime('%H:%M')} IST. {shorting}

How to work
- Your own book is listed above, before you are asked anything: what you are
  holding, and how the day has gone so far. Call `orders` for the detail behind
  it. Two things follow from reading it. Nothing in this framework stops you
  opening a second position in a stock you are already in, so check before you
  add. And your results are evidence about how you are reading this tape - a
  run of losses is something to look at, not something to trade your way out of.
- Screen the whole universe first, then inspect two or three names that
  survived. Do not inspect all fifty: the screen is there to make that
  unnecessary.
- Ask for the names you are considering in one go rather than one after another:
  everything you ask for in a single turn is fetched together.
- Ground every claim in a field you were actually shown, and name it.
- Then call submit_decision once for each name worth trading - an order with a
  symbol, a target, a stop, a holding time and a reason - and again with
  action='ignore' if the candle is not worth acting on. More than one order in a
  cycle is allowed and expected. Prose alone will not be read.
- The reason is the audit note, not the essay: one or two sentences, at most 50
  words, naming the levels and fields that convinced you. A longer one is
  refused and has to be submitted again.
- If a decision is refused, the refusal says which rule was broken. Correct it
  and submit again, or ignore the candle.

Your notebook
- You keep a small notebook (the notes tool). It is the only memory that
  crosses cycles, restarts and nights; nothing else you think is remembered.
  It is shown to you at the start of every cycle, so read it as your own past
  reasoning, not as the framework's advice.
- One key is one lesson. Write it evidence-shaped: the condition you saw, what
  you did, and how it turned out - with a count when you have one. "Be more
  careful" is not a lesson; "ORL breaks with volume_ratio < 1: 3 losses in 12
  trades" is.
- write replaces one key, append extends it, delete removes it. The notebook is
  bounded, so delete what stopped being true rather than letting it crowd out
  the rest. Do not restate what the book already tells you - the rupees, the R
  multiples and your open positions are given to you every cycle.
- **After your decisions are made and before you sleep, update the notebook.
  It is mandatory**: one write, append or delete that says what this candle
  taught you. A cycle that decides and does not record is a cycle tomorrow
  cannot learn from.
- **On the final cycle of the day, write the whole-day lesson.** Keep the
  evidence that still holds, remove what the day disproved, and leave one
  durable lesson that tomorrow's first cycle should act on. The framework
  checkpoints the notebook at the close; that checkpoint is your memory across
  trading days.

The operator's instructions follow, and they take precedence over anything
above that is merely a default.

--- BEGIN OPERATOR INSTRUCTIONS ---
{definition.system_prompt}
--- END OPERATOR INSTRUCTIONS ---
"""


def build_messages(
    definition: AgentDefinition,
    *,
    bar_open_time: str,
    now: datetime,
    book: str = "",
    notes: str = "",
    note: str | None = None,
    window: TradingWindow | None = None,
    bar_close_at: datetime | None = None,
    end_of_day: bool = False,
) -> list[dict[str, Any]]:
    """The whole request: the stable system half, then this moment's facts.

    ``book`` is what the agent is already holding, fetched by the worker before
    the model is asked anything. It used to cost a whole round - a model call,
    seconds - for the agent to find out, and the round it cost was the one where
    it decided whether to add to a position it had forgotten.

    ``notes`` is the agent's own notebook, and it goes in the volatile half on
    purpose: the fixed half stays byte-identical between cycles so the
    provider's prompt cache keeps working, and the notebook - which changes
    only when the agent writes to it - travels with the candle it is read
    against.
    """

    held = window or TradingWindow()
    anchor = bar_close_at or now
    room = max(0, int((held.closes_at(now) - anchor).total_seconds() // 60))
    lines = [
        f"The {definition.timeframe} candle that just closed opened at "
        f"{bar_open_time}.",
        f"It is now {format_ist(now)}. Your agent id is {definition.agent_id}.",
        (
            f"The framework closes at {held.close.strftime('%H:%M')} IST. From the candle "
            f"you are answering, at most {room} minutes of holding time remain."
        ),
    ]
    if book:
        lines.append(book)
    if notes:
        lines.append(notes)
    lines.append(
        "Screen the universe, inspect the survivors, then call submit_decision. "
        "After your decisions, update your notebook with the notes tool before "
        "the cycle ends - it is mandatory."
    )
    if note:
        lines.append(note)
    if end_of_day:
        lines.append(
            "This is the final decision cycle of the day. Finish with one notebook "
            "update that carries the most useful whole-day lesson into tomorrow; "
            "the checkpoint is automatic after this cycle."
        )
    return [
        {"role": "system", "content": system_message(definition, window=held)},
        {"role": "user", "content": " ".join(lines)},
    ]

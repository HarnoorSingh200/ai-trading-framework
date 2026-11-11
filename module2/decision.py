"""What may become an order, and how big it may be.

Two jobs, kept apart from the model on purpose.

**The rules** are what a proposal must satisfy to be an order at all: a target
and a stop on the right sides of a live price, a holding time the agent is
allowed, an exit that lands before the session ends, a reason. A failure is not
an exception - it is a list of problems that the model is shown, so it can
correct itself inside the same cycle if it can.

**The arithmetic** is the quantity. The model does not propose one: a size is
``risk budget ÷ stop distance``, capped by the notional the agent's capital and
leverage allow, and a number the model made up is the one field that could do
real damage. Every rupee of it is reported back in the record so the number can
be re-derived later rather than taken on trust.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from module2.definition import AgentDefinition
from module2.timeutil import IST, to_ist

#: The three things a decision may say.
ACTION_BUY: Final[str] = "buy"
ACTION_SELL: Final[str] = "sell"
ACTION_IGNORE: Final[str] = "ignore"
ACTIONS: Final[tuple[str, ...]] = (ACTION_BUY, ACTION_SELL, ACTION_IGNORE)

#: The last moment an intraday position may be closed. The framework's own
#: close is 14:50 by default; an exit planned after that is an exit the
#: operator's day does not allow. The order manager enforces the same moment
#: from its own side.
LAST_EXIT: Final[time] = time(14, 50)

_CENT: Final[Decimal] = Decimal("0.01")

#: How long the written reason may be. The reason is the audit note a person
#: reads later - the model's own reasoning is not written here, and should not
#: be: fifty words is enough to name the level and the fields around it. The
#: limit is enforced rather than asked for, because a prompt instruction the
#: model can ignore is not a limit.
REASON_WORD_LIMIT: Final[int] = 50


class DecisionError(ValueError):
    """A decision that could not be read at all."""


@dataclass(frozen=True, slots=True)
class Problem:
    """One rule that was not satisfied, with the sentence the model is shown."""

    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class Proposal:
    """What the model asked for, read and typed but not yet judged."""

    action: str
    reason: str
    symbol: str | None = None
    target: Decimal | None = None
    stop_loss: Decimal | None = None
    max_hold_minutes: int | None = None
    confidence: float | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "symbol": self.symbol,
            "target": None if self.target is None else float(self.target),
            "stop_loss": None if self.stop_loss is None else float(self.stop_loss),
            "max_hold_minutes": self.max_hold_minutes,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class Checked:
    """The verdict on a proposal, and the size when there is one."""

    ok: bool
    problems: tuple[Problem, ...] = ()
    quantity: int = 0
    entry_price: Decimal | None = None
    price_source: str | None = None
    sizing: dict[str, Any] = field(default_factory=dict)

    @property
    def messages(self) -> list[str]:
        return [problem.message for problem in self.problems]


def _price(raw: Any, *, field_name: str) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise DecisionError(f"{field_name} must be a price")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DecisionError(f"{field_name} must be a price, not {raw!r}") from exc
    if not value.is_finite() or value <= 0:
        raise DecisionError(f"{field_name} must be a positive price, not {value}")
    return value


def proposal_from_args(args: Mapping[str, Any]) -> Proposal:
    """Read the terminal tool's arguments, refusing anything unreadable."""

    if not isinstance(args, Mapping):
        raise DecisionError("a decision must be an object")
    action = str(args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise DecisionError(
            f"action must be one of {', '.join(ACTIONS)}, not {args.get('action')!r}"
        )
    if "reason" not in args or args.get("reason") is None:
        raise DecisionError("every decision needs a reason")
    reason = str(args.get("reason"))
    words = len(reason.split())
    if words > REASON_WORD_LIMIT:
        raise DecisionError(
            f"the reason is {words} words; keep it to {REASON_WORD_LIMIT} words "
            "or fewer, naming the fields that convinced you"
        )

    confidence: float | None = None
    if args.get("confidence") is not None:
        if isinstance(args["confidence"], bool):
            raise DecisionError("confidence must be a number between 0 and 1")
        try:
            confidence = float(args["confidence"])
        except (TypeError, ValueError) as exc:
            raise DecisionError("confidence must be a number between 0 and 1") from exc
        if not 0.0 <= confidence <= 1.0:
            raise DecisionError("confidence must be between 0 and 1")

    if action == ACTION_IGNORE:
        return Proposal(action=action, reason=reason, confidence=confidence)

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        raise DecisionError("an order needs a symbol")
    hold: int | None = None
    if args.get("max_hold_minutes") is not None:
        if isinstance(args["max_hold_minutes"], bool):
            raise DecisionError("max_hold_minutes must be a whole number of minutes")
        try:
            hold = int(str(args["max_hold_minutes"]).strip())
        except (TypeError, ValueError) as exc:
            raise DecisionError(
                "max_hold_minutes must be a whole number of minutes"
            ) from exc
        if hold < 1:
            raise DecisionError("max_hold_minutes must be at least one minute")
    return Proposal(
        action=action,
        reason=reason,
        symbol=symbol,
        target=_price(args.get("target"), field_name="target"),
        stop_loss=_price(args.get("stop_loss"), field_name="stop_loss"),
        max_hold_minutes=hold,
        confidence=confidence,
    )


def size(
    *, entry: Decimal, stop: Decimal, definition: AgentDefinition
) -> tuple[int, dict[str, Any]]:
    """Whole shares that fit inside both the risk budget and the notional cap.

    ``0`` is a real answer: sometimes the stop is wide enough, or the share
    expensive enough, that the smallest position would breach a limit, and the
    honest response to that is not to take the trade.
    """

    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        raise DecisionError(
            "the stop is on top of the entry, so there is no risk to size against"
        )
    by_risk = int(definition.risk_per_trade // risk_per_share)
    by_notional = int(definition.max_notional // entry) if entry > 0 else 0
    quantity = max(0, min(by_risk, by_notional))
    return quantity, {
        "entry": str(entry),
        "stop": str(stop),
        "risk_per_share": str(risk_per_share),
        "risk_budget": str(definition.risk_per_trade.quantize(_CENT)),
        "notional_cap": str(definition.max_notional.quantize(_CENT)),
        "by_risk": by_risk,
        "by_notional": by_notional,
        "quantity": quantity,
        "money_at_risk": str((risk_per_share * quantity).quantize(_CENT)),
        "notional": str((entry * quantity).quantize(_CENT)),
    }


def check(
    proposal: Proposal,
    *,
    definition: AgentDefinition,
    bundle: Mapping[str, Any] | None,
    now: datetime,
    session_open: bool,
    stale_price_seconds: float = 120.0,
    close_at: datetime | None = None,
    bar_close_at: datetime | None = None,
) -> Checked:
    """Judge a proposal, and size it when it stands up.

    ``close_at`` is the framework's own close (14:50 IST by default) and
    ``bar_close_at`` the candle the decision was made on. The holding time is
    counted from the candle, because that is the moment the decision belongs to
    and the moment the position is entered a second or two after - so a plan
    made on the 14:30 candle may still ask for the twenty minutes that reach
    14:50. When no close is given the framework's default close applies, which
    keeps this function honest when it is called on its own.
    """

    problems: list[Problem] = []
    if not proposal.reason.strip():
        problems.append(
            Problem("reason", "a decision needs a reason: say what you saw and why it matters")
        )

    if proposal.action == ACTION_IGNORE:
        # Doing nothing is always allowed - it is the one decision that needs no
        # price, no size and no open market.
        return Checked(ok=not problems, problems=tuple(problems))

    if not session_open:
        problems.append(
            Problem(
                "market_closed",
                "the market is closed, so no order may be placed; decide again on "
                "the next candle of an open session",
            )
        )
    if bundle is None:
        problems.append(
            Problem(
                "symbol",
                f"{proposal.symbol} has no finished candle to decide on, so it is not "
                "in a state this agent can trade",
            )
        )
        return Checked(ok=False, problems=tuple(problems))

    day = bundle.get("day") or {}
    raw_price = day.get("last_price")
    source = day.get("price_source")
    age = day.get("price_age_seconds")
    entry: Decimal | None = None
    if raw_price is None or source == "none":
        problems.append(
            Problem("price", f"{proposal.symbol} has no price at all right now")
        )
    else:
        entry = Decimal(str(raw_price))
        if source != "live":
            problems.append(
                Problem(
                    "price",
                    f"{proposal.symbol}'s price is a stored '{source}' rather than a live "
                    "tick, and a position must not be sized on a stale reference",
                )
            )
        elif age is not None and float(age) > float(stale_price_seconds):
            problems.append(
                Problem(
                    "price",
                    f"{proposal.symbol}'s last tick is {float(age):.0f}s old, older than the "
                    f"{float(stale_price_seconds):.0f}s this agent will act on",
                )
            )

    target, stop = proposal.target, proposal.stop_loss
    if entry is not None and target is not None and stop is not None:
        if proposal.action == ACTION_BUY and not stop < entry < target:
            problems.append(
                Problem(
                    "target_and_stop",
                    f"a buy needs the stop below the price and the target above it: "
                    f"stop {stop}, price {entry}, target {target}",
                )
            )
        if proposal.action == ACTION_SELL and not target < entry < stop:
            problems.append(
                Problem(
                    "target_and_stop",
                    f"a short needs the target below the price and the stop above it: "
                    f"target {target}, price {entry}, stop {stop}",
                )
            )

    if proposal.action == ACTION_SELL and not definition.allow_short:
        problems.append(
            Problem("short_not_allowed", "this agent is configured to trade long only")
        )

    hold = proposal.max_hold_minutes or definition.max_hold_minutes
    if hold > definition.max_hold_minutes:
        problems.append(
            Problem(
                "hold",
                f"{hold} minutes is longer than this agent holds ({definition.max_hold_minutes})",
            )
        )
    anchor = bar_close_at or now
    exit_at = anchor + timedelta(minutes=hold)
    deadline = close_at or datetime.combine(to_ist(now).date(), LAST_EXIT, tzinfo=IST)
    if exit_at > deadline:
        room = max(0, int((deadline - anchor).total_seconds() // 60))
        problems.append(
            Problem(
                "exit_time",
                f"holding {hold} minutes from the "
                f"{to_ist(anchor).strftime('%H:%M')} candle would close at "
                f"{to_ist(exit_at).strftime('%H:%M')} IST, and this framework takes "
                f"no position past {to_ist(deadline).strftime('%H:%M')} IST"
                + (
                    f"; from that candle the longest holding time is {room} minutes"
                    if room
                    else ""
                ),
            )
        )

    if problems or entry is None or stop is None:
        return Checked(ok=False, problems=tuple(problems), entry_price=entry, price_source=source)

    quantity, sizing = size(entry=entry, stop=stop, definition=definition)
    if quantity < 1:
        problems.append(
            Problem(
                "size",
                f"a stop {abs(entry - stop)} away on a {entry} share does not fit one whole "
                f"share inside the risk budget ({sizing['risk_budget']}) and the notional "
                f"cap ({sizing['notional_cap']})",
            )
        )
        return Checked(ok=False, problems=tuple(problems), entry_price=entry, price_source=source)
    return Checked(
        ok=True,
        quantity=quantity,
        entry_price=entry,
        price_source=source,
        sizing=sizing,
    )

"""What an order is, and the only ways it is allowed to change.

Module 2 hands over a decision; this is what that decision becomes once the
framework is responsible for it. An order is a frozen record, and every change
produces a new one through an explicit transition - so an order cannot quietly
become something it never was, and the ledger can be replayed to prove it.

The lifecycle is deliberately small, because Module 3 cannot reach the broker:

* ``pending``  - accepted, and waiting for Module 1 to confirm a *live* price.
* ``open``     - executed; the target, the stop and the clock are watched.
* ``closed``   - finished, with the reason it finished.
* ``expired``  - it never became a position inside the window it was given.

Two things here exist to stop the module flattering itself, and both are
recorded in the order rather than left to be inferred later:

**A fill happens at the first live price Module 1 publishes for the stock.** The
decision's own entry price is what the plan was built on, not where the position
was taken, so it is kept beside the fill as ``decision_entry_price`` and the
distance between the two is recorded. Everything downstream - the money at risk,
the notional, and the P&L of a finished order - is computed from the price the
position was really taken at.

Two rules stop that becoming a licence to enter anywhere. **The plan must still
bracket the market**: a buy cannot be filled at or above its target, or at or
below its stop, because by then there is no trade left to take. And **the plan
must not be stale**: past the operator's tolerance the market has moved far
enough that these are no longer the levels that were decided on, and the order
expires instead of entering.

**When one price moves across both the target and the stop, the stop is the
outcome that counts.** A snapshot feed carries no ordering inside the gap
between two observations, so which level was touched first is genuinely
unknowable. Booking the profitable one would be the single most flattering
mistake this module could make, and the flag ``ambiguous_exit`` says out loud
that the answer was chosen against the position.

**A finished order carries its own P&L.** When an order settles, the rupees are
not the end of it: what it made, what that was against the capital it used, what
the plan had at risk, and the R-multiple the result represents are all stored
with it, so a trade can be judged rather than merely totalled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from module3.timeutil import format_utc, parse_timestamp, to_ist

#: The four states an order can be in.
STATUS_PENDING: Final[str] = "pending"
STATUS_OPEN: Final[str] = "open"
STATUS_CLOSED: Final[str] = "closed"
STATUS_EXPIRED: Final[str] = "expired"

STATUSES: Final[tuple[str, ...]] = (
    STATUS_PENDING,
    STATUS_OPEN,
    STATUS_CLOSED,
    STATUS_EXPIRED,
)

#: A status from which nothing further is expected.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({STATUS_CLOSED, STATUS_EXPIRED})

#: The two sides a decision may take. Standing aside is neither, and is not an
#: order at all - see :func:`order_from_decision`.
SIDE_BUY: Final[str] = "buy"
SIDE_SELL: Final[str] = "sell"
SIDES: Final[tuple[str, ...]] = (SIDE_BUY, SIDE_SELL)

#: Why an order stopped. There is no "session end" reason on purpose: every
#: holding time is bounded by the exit deadline Module 2 already enforces, so
#: the clock can only ever run out, and a second calendar here would be one
#: more place for exchange holidays to be modelled wrongly.
EXIT_TARGET: Final[str] = "target"
EXIT_STOP: Final[str] = "stop_loss"
EXIT_TIMEOUT: Final[str] = "timeout"
EXIT_REASONS: Final[tuple[str, ...]] = (EXIT_TARGET, EXIT_STOP, EXIT_TIMEOUT)

#: How a settled order turned out.
OUTCOME_WIN: Final[str] = "win"
OUTCOME_LOSS: Final[str] = "loss"
OUTCOME_FLAT: Final[str] = "flat"

_CENT: Final[Decimal] = Decimal("0.01")
#: Ratios keep four places: an R-multiple that rounds to two turns 1.005R and
#: 1.004R into the same number, and the difference is a real one on a sample of
#: a hundred trades.
_RATIO: Final[Decimal] = Decimal("0.0001")


class OrderError(ValueError):
    """A decision that cannot become an order, naming the field at fault."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class Exit:
    """The exit an order's own numbers imply, before it is acted upon."""

    reason: str
    price: Decimal
    ambiguous: bool = False
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Pnl:
    """What a finished order made, in the terms a trade is judged in.

    Rupees say what happened; they do not say whether it was worth doing. A
    hundred-rupee win that risked forty is a different trade from one that
    risked four hundred, which is what the R-multiple is for: it measures the
    result in the risk the plan actually carried at the price the position was
    taken at.

    Every number here is derived from the entry the order really got, not from
    the price the decision was made at, so it describes the trade that was taken
    rather than the one that was planned.
    """

    gross: Decimal
    capital: Decimal
    percent: Decimal
    risk: Decimal
    planned_reward: Decimal
    r_multiple: Decimal | None
    reward_risk: Decimal | None
    outcome: str

    def describe(self) -> dict[str, Any]:
        return {
            "gross": _number(self.gross),
            "capital": _number(self.capital),
            "percent": _number(self.percent, places=4),
            "risk": _number(self.risk),
            "planned_reward": _number(self.planned_reward),
            "r_multiple": (
                None if self.r_multiple is None else _number(self.r_multiple, places=4)
            ),
            "reward_risk": (
                None if self.reward_risk is None else _number(self.reward_risk, places=4)
            ),
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Pnl | None:
        """Rebuild a settlement, or ``None`` when it was never written."""

        if not isinstance(payload, Mapping):
            return None
        try:
            return cls(
                gross=Decimal(str(payload["gross"])),
                capital=Decimal(str(payload["capital"])),
                percent=Decimal(str(payload["percent"])),
                risk=Decimal(str(payload["risk"])),
                planned_reward=Decimal(str(payload["planned_reward"])),
                r_multiple=_optional_decimal(payload.get("r_multiple")),
                reward_risk=_optional_decimal(payload.get("reward_risk")),
                outcome=str(payload["outcome"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None


@dataclass(frozen=True, slots=True)
class Order:
    """One decision, as the framework is holding it."""

    order_id: str
    decision_id: str
    agent_id: str
    symbol: str
    side: str
    quantity: int
    #: The price the position was taken at. Until the order fills this is the
    #: price the plan was built on; from the fill onwards it is the live price
    #: the position was actually entered at, and every number below is measured
    #: from it.
    entry_price: Decimal
    #: Where the plan was built. Never changes, so the two together say what the
    #: market did between the decision and the entry.
    decision_entry_price: Decimal
    target: Decimal
    stop_loss: Decimal
    max_hold_minutes: int
    reason: str
    status: str = STATUS_PENDING
    created_at: datetime | None = None
    received_at: datetime | None = None
    #: The exchange day this order was taken on, in IST. It is what makes a
    #: daily book a lookup rather than a re-derivation: an order belongs to the
    #: session it was decided in, and that never changes afterwards.
    session_date: str | None = None
    #: The moment the operator's framework closes. The holding time may not run
    #: past it, so the last order of the day is the one that reaches the close
    #: rather than one that has to be guessed about afterwards.
    session_close_at: datetime | None = None
    filled_at: datetime | None = None
    ended_at: datetime | None = None
    exit_reason: str | None = None
    exit_price: Decimal | None = None
    exit_note: str | None = None
    ambiguous_exit: bool = False
    fill_drift_pct: float | None = None
    #: The settlement, present once the order has finished.
    pnl: Pnl | None = None
    timeframe: str | None = None
    bar_open_time: str | None = None
    model: str | None = None
    confidence: float | None = None
    decision_price_source: str | None = None

    # -- the numbers that make the trade auditable --------------------------- #

    @property
    def notional(self) -> Decimal:
        """What the position is worth at the price it was taken at."""

        return self.entry_price * self.quantity

    @property
    def risk_per_share(self) -> Decimal:
        return abs(self.entry_price - self.stop_loss)

    @property
    def risk(self) -> Decimal:
        """The money at risk if the stop is reached, as planned."""

        return self.risk_per_share * self.quantity

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    @property
    def is_live(self) -> bool:
        """Whether this order still needs watching."""

        return self.status in {STATUS_PENDING, STATUS_OPEN}

    @property
    def deadline(self) -> datetime | None:
        """When the holding time runs out, counted from the fill.

        The clock starts at the fill, not at the decision: the time a trade was
        given is time it was actually exposed to the market, and an order that
        waited for its price was not exposed yet.

        It also stops at the framework's close. A plan that asked for an hour at
        14:10 has fifty minutes of market left, not sixty, and the book says so
        rather than letting the position outlive the day the operator set.
        """

        if self.filled_at is None:
            return None
        natural = self.filled_at + timedelta(minutes=self.max_hold_minutes)
        if self.session_close_at is None:
            return natural
        return min(natural, self.session_close_at)

    def pnl_at(self, price: Decimal) -> Decimal:
        """The money this position makes or loses at ``price``."""

        move = price - self.entry_price if self.side == SIDE_BUY else self.entry_price - price
        return (move * self.quantity).quantize(_CENT)

    # -- serialisation ------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "decision_id": self.decision_id,
            "agent_id": self.agent_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": _number(self.entry_price),
            "decision_entry_price": _number(self.decision_entry_price),
            "target": _number(self.target),
            "stop_loss": _number(self.stop_loss),
            "max_hold_minutes": self.max_hold_minutes,
            "reason": self.reason,
            "status": self.status,
            "notional": _number(self.notional),
            "risk": _number(self.risk),
            "risk_per_share": _number(self.risk_per_share),
            "created_at": _stamp(self.created_at),
            "received_at": _stamp(self.received_at),
            "session_date": self.session_date,
            "session_close_at": _stamp(self.session_close_at),
            "filled_at": _stamp(self.filled_at),
            "ended_at": _stamp(self.ended_at),
            "deadline": _stamp(self.deadline),
            "exit_reason": self.exit_reason,
            "exit_price": _number(self.exit_price),
            "exit_note": self.exit_note,
            "ambiguous_exit": self.ambiguous_exit,
            "fill_drift_pct": self.fill_drift_pct,
            "pnl": None if self.pnl is None else self.pnl.describe(),
            "timeframe": self.timeframe,
            "bar_open_time": self.bar_open_time,
            "model": self.model,
            "confidence": self.confidence,
            "decision_price_source": self.decision_price_source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Order:
        """Rebuild an order from what was written to disk."""

        return cls(
            order_id=str(payload["order_id"]),
            decision_id=str(payload["decision_id"]),
            agent_id=str(payload["agent_id"]),
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            quantity=int(payload["quantity"]),
            entry_price=_decimal(str(payload["entry_price"])),
            decision_entry_price=_decimal(
                str(payload.get("decision_entry_price", payload["entry_price"]))
            ),
            target=_decimal(str(payload["target"])),
            stop_loss=_decimal(str(payload["stop_loss"])),
            max_hold_minutes=int(payload["max_hold_minutes"]),
            reason=str(payload.get("reason") or ""),
            status=str(payload.get("status") or STATUS_PENDING),
            created_at=parse_timestamp(payload.get("created_at")),
            received_at=parse_timestamp(payload.get("received_at")),
            session_date=_optional_text(payload.get("session_date"))
            or _session_day_of(payload),
            session_close_at=parse_timestamp(payload.get("session_close_at")),
            filled_at=parse_timestamp(payload.get("filled_at")),
            ended_at=parse_timestamp(payload.get("ended_at")),
            exit_reason=_optional_text(payload.get("exit_reason")),
            exit_price=_optional_decimal(payload.get("exit_price")),
            exit_note=_optional_text(payload.get("exit_note")),
            ambiguous_exit=bool(payload.get("ambiguous_exit")),
            fill_drift_pct=_optional_float(payload.get("fill_drift_pct")),
            pnl=Pnl.from_dict(payload.get("pnl")),
            timeframe=_optional_text(payload.get("timeframe")),
            bar_open_time=_optional_text(payload.get("bar_open_time")),
            model=_optional_text(payload.get("model")),
            confidence=_optional_float(payload.get("confidence")),
            decision_price_source=_optional_text(payload.get("decision_price_source")),
        )


# -- intake ------------------------------------------------------------------ #


def order_from_decision(
    payload: Mapping[str, Any],
    *,
    now: datetime,
    order_id: str,
    session_close_at: datetime | None = None,
) -> Order:
    """Read a decision, or refuse it naming the field that is wrong.

    Everything Module 2 already guarantees is checked again here. That is not
    distrust for its own sake: this is the module that will act on these
    numbers, and a boundary that only trusts its caller is not a boundary.
    """

    if not isinstance(payload, Mapping):
        raise OrderError("an order must come from a decision object")

    action = str(payload.get("action") or "").strip().lower()
    if action not in SIDES:
        raise OrderError(
            f"{action or 'nothing'!r} is not a side; only a buy or a sell becomes an order",
            field="action",
        )

    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise OrderError("an order needs a symbol", field="symbol")

    decision_id = str(payload.get("decision_id") or "").strip()
    if not decision_id:
        raise OrderError(
            "an order needs the decision it came from, or it cannot be traced "
            "back to why it exists",
            field="decision_id",
        )

    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id:
        raise OrderError("an order needs the agent that decided it", field="agent_id")

    quantity = _whole(payload.get("quantity"), "quantity")
    if quantity < 1:
        raise OrderError(
            f"a decision sized at {quantity} shares is not an order", field="quantity"
        )

    entry = _price(payload.get("entry_price"), "entry_price")
    target = _price(payload.get("target"), "target")
    stop = _price(payload.get("stop_loss"), "stop_loss")
    hold = _whole(payload.get("max_hold_minutes"), "max_hold_minutes")
    if hold < 1:
        raise OrderError(
            "an order needs a holding time of at least one minute",
            field="max_hold_minutes",
        )

    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise OrderError("an order needs the reason it was taken", field="reason")

    # The ordering of the three levels is the difference between a plan and a
    # hope: a target on the wrong side of the entry is a position that can only
    # lose, and a stop there is one that can only win.
    if action == SIDE_BUY and not stop < entry < target:
        raise OrderError(
            "a buy needs the stop below the entry and the target above it: "
            f"stop {stop}, entry {entry}, target {target}",
            field="target",
        )
    if action == SIDE_SELL and not target < entry < stop:
        raise OrderError(
            "a sell needs the target below the entry and the stop above it: "
            f"target {target}, entry {entry}, stop {stop}",
            field="target",
        )

    return Order(
        order_id=order_id,
        decision_id=decision_id,
        agent_id=agent_id,
        symbol=symbol,
        side=action,
        quantity=quantity,
        entry_price=entry,
        decision_entry_price=entry,
        target=target,
        stop_loss=stop,
        max_hold_minutes=hold,
        reason=reason,
        created_at=parse_timestamp(payload.get("created_at")) or now,
        received_at=now,
        session_date=to_ist(now).date().isoformat(),
        session_close_at=session_close_at,
        timeframe=_optional_text(payload.get("timeframe")),
        bar_open_time=_optional_text(payload.get("bar_open_time")),
        model=_optional_text(payload.get("model")),
        confidence=_confidence(payload.get("confidence")),
        decision_price_source=_optional_text(payload.get("price_source")),
    )


# -- transitions ------------------------------------------------------------- #


def plan_holds_at(order: Order, price: Decimal) -> bool:
    """Whether the target and stop still bracket the price an order would enter at.

    A plan is a pair of levels around an entry. Once the market is at or past
    one of them there is no trade left to take: a buy entered above its target
    has no room to gain, and one entered below its stop was stopped out before
    it began.
    """

    if order.side == SIDE_BUY:
        return order.stop_loss < price < order.target
    return order.target < price < order.stop_loss


def fill(
    order: Order, *, at: datetime, price: Decimal, drift_pct: float | None = None
) -> Order:
    """Execute the order at the price Module 1 is publishing. ``pending`` -> ``open``.

    The price the position is taken at becomes the order's entry, and every
    number derived from it - the money at risk, the notional, the P&L at the end
    - is recomputed from that rather than from the price the plan was made at.
    """

    _require(order, STATUS_PENDING, "be filled")
    return replace(
        order,
        status=STATUS_OPEN,
        filled_at=at,
        entry_price=price,
        fill_drift_pct=drift_pct,
    )


def close(order: Order, exit_: Exit, *, at: datetime) -> Order:
    """Finish an open order and settle it. ``open`` -> ``closed``."""

    _require(order, STATUS_OPEN, "be closed")
    if exit_.reason not in EXIT_REASONS:
        raise OrderError(f"{exit_.reason!r} is not an exit this module produces")
    return replace(
        order,
        status=STATUS_CLOSED,
        ended_at=at,
        exit_reason=exit_.reason,
        exit_price=exit_.price,
        exit_note=exit_.note,
        ambiguous_exit=exit_.ambiguous,
        # The trade is over: what it made, and what that was against the risk it
        # carried, are settled here rather than recomputed by whoever reads it.
        pnl=pnl_summary(order, exit_.price),
    )


def expire(order: Order, *, at: datetime, note: str) -> Order:
    """Give up on an order that never became a position.

    An order that is still pending when its own holding window has passed is not
    a position anyone is exposed to; it is a record of a trade that was planned
    and never happened, and saying so is more useful than leaving it open for
    ever.
    """

    _require(order, STATUS_PENDING, "expire")
    return replace(order, status=STATUS_EXPIRED, ended_at=at, exit_note=note)


def _require(order: Order, status: str, verb: str) -> None:
    if order.status != status:
        raise OrderError(
            f"order {order.order_id} is {order.status}, so it cannot {verb}",
            field="status",
        )


# -- watching ---------------------------------------------------------------- #


def evaluate(
    order: Order,
    *,
    price: Decimal,
    at: datetime,
    now: datetime,
    previous_price: Decimal | None = None,
) -> Exit | None:
    """Which exit, if any, this price produces for this order.

    ``at`` is when the price was observed and ``now`` is the current moment.
    They differ, and the difference matters: a price observed after the holding
    deadline is late news, and booking an exit against it would credit the
    position with a market it was no longer in.
    """

    if order.status != STATUS_OPEN:
        return None

    deadline = order.deadline
    within_window = deadline is None or at <= deadline
    if within_window:
        stop_hit, target_hit = _hits(order, price)
        if stop_hit or target_hit:
            ambiguous = previous_price is not None and _crossed_both(
                order, previous_price, price
            )
            reason = EXIT_STOP if (ambiguous or stop_hit) else EXIT_TARGET
            return Exit(
                reason=reason,
                price=_exit_price(order, reason, price),
                ambiguous=ambiguous,
                note=(
                    "the price moved across both the target and the stop between "
                    "two observations, so there is no honest way to say which was "
                    "reached first; the stop is the one that counts"
                    if ambiguous
                    else None
                ),
            )

    if deadline is not None and now >= deadline:
        late = at > deadline
        return Exit(
            reason=EXIT_TIMEOUT,
            price=price,
            note=(
                "the last price was observed after the holding deadline, so the "
                "exit is booked against the last market this order was known to "
                "be in"
                if late
                else None
            ),
        )
    return None


def _hits(order: Order, price: Decimal) -> tuple[bool, bool]:
    """``(stop reached, target reached)`` at this one price."""

    if order.side == SIDE_BUY:
        return price <= order.stop_loss, price >= order.target
    return price >= order.stop_loss, price <= order.target


def _crossed_both(order: Order, previous: Decimal, current: Decimal) -> bool:
    """Whether one move took the price past the target *and* the stop."""

    stop_now, target_now = _hits(order, current)
    stop_then, target_then = _hits(order, previous)
    if stop_now and target_now:
        return True
    return (target_now and stop_then) or (stop_now and target_then)


def _exit_price(order: Order, reason: str, price: Decimal) -> Decimal:
    """What the exit is booked at.

    A target is never booked better than the target, because a snapshot cannot
    say which side of a gap a fill would have landed on and assuming the
    favourable one is how a paper system lies. A stop is booked at the level or
    worse, because a gap through a stop fills worse and that is the number the
    trade actually cost.
    """

    if reason == EXIT_TARGET:
        return order.target
    if reason == EXIT_STOP:
        if order.side == SIDE_BUY:
            return min(price, order.stop_loss)
        return max(price, order.stop_loss)
    return price


def pnl_summary(order: Order, exit_price: Decimal) -> Pnl:
    """Settle a finished order: what it made, and what that was worth.

    Every figure comes from the entry the order actually got. The risk is the
    one the plan carried *at that entry*, which is the only risk the position
    ever had - so the R-multiple measures the result against something real
    rather than against the arithmetic of a price the trade never paid.
    """

    gross = order.pnl_at(exit_price)
    capital = (order.entry_price * order.quantity).quantize(_CENT)
    risk = (order.risk_per_share * order.quantity).quantize(_CENT)
    planned_reward = (abs(order.target - order.entry_price) * order.quantity).quantize(
        _CENT
    )
    percent = _percent_of(gross, capital)
    if gross > 0:
        outcome = OUTCOME_WIN
    elif gross < 0:
        outcome = OUTCOME_LOSS
    else:
        outcome = OUTCOME_FLAT
    return Pnl(
        gross=gross,
        capital=capital,
        percent=Decimal("0") if percent is None else percent,
        risk=risk,
        planned_reward=planned_reward,
        r_multiple=_multiple(gross, risk),
        reward_risk=_multiple(planned_reward, risk),
        outcome=outcome,
    )


def _percent_of(top: Decimal, bottom: Decimal) -> Decimal | None:
    if bottom == 0:
        return None
    return (top / bottom * Decimal("100")).quantize(_RATIO)


def _multiple(top: Decimal, bottom: Decimal) -> Decimal | None:
    if bottom == 0:
        return None
    return (top / bottom).quantize(_RATIO)


# -- reading values off a decision ------------------------------------------- #


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(str(text).strip())
    except (InvalidOperation, ValueError) as exc:
        raise OrderError(f"{text!r} is not a number") from exc


def _price(raw: Any, field: str) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise OrderError(f"{field} must be a price", field=field)
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise OrderError(f"{field} must be a price, not {raw!r}", field=field) from exc
    if not value.is_finite() or value <= 0:
        raise OrderError(f"{field} must be a positive price, not {value}", field=field)
    return value


def _whole(raw: Any, field: str) -> int:
    if raw is None or isinstance(raw, bool):
        raise OrderError(f"{field} must be a whole number", field=field)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise OrderError(
            f"{field} must be a whole number, not {raw!r}", field=field
        ) from exc


def _confidence(raw: Any) -> float | None:
    """The model's own confidence, kept only when it is a real one."""

    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def _optional_text(raw: Any) -> str | None:
    text = str(raw).strip() if raw is not None else ""
    return text or None


def _session_day_of(payload: Mapping[str, Any]) -> str | None:
    """The exchange day an order belongs to, when the record does not say.

    ``session_date`` was added after orders had already been written, and a book
    whose older rows carry no day is a book that reports nothing for them: the
    daily index comes back empty and the archive has nothing to file. So it is
    derived - from when the order was actually received, never from today, since
    stamping an old trade with this morning would be worse than leaving it
    undated.
    """

    for key in ("received_at", "created_at", "filled_at"):
        moment = parse_timestamp(payload.get(key))
        if moment is not None:
            return to_ist(moment).date().isoformat()
    return None


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None


def _optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _number(value: Decimal | None, *, places: int = 2) -> float | None:
    return None if value is None else float(round(value, places))


def _stamp(value: datetime | None) -> str | None:
    """A timestamp in the form every module here writes: UTC, with a ``Z``.

    Normalising on the way out is not cosmetic. The ledger is rewritten on every
    transition, and a record that serialised differently depending on which
    timezone it happened to be created in would make two readings of one instant
    compare unequal - which is exactly the kind of drift a replay is meant to
    rule out.
    """

    return None if value is None else format_utc(value)

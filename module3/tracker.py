"""Orders, as the framework holds and moves them.

This is where a decision stops being an intention. An order arrives, waits for
Module 1 to publish a live price for its stock, is executed at that price, and
is then watched against three things at once - its target, its stop and its
clock - until one of them ends it.

Every mutation goes through :mod:`module3.orders`, so an order can only move
along a transition that exists, and every move is appended to the ledger before
it is returned. Nothing here is written twice: an order's state is derived from
the transitions it has been through, and the event log records each one.

Several orders can be open in the same stock at once, including from different
agents and in opposite directions. They are tracked independently and never
netted: two agents that disagree are two positions, and merging them into one
number would destroy the only thing that makes either of them judgeable.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from common.telegram import Notifier, NullNotifier, escape
from common.window import TradingWindow
from module3.clients import MarketClient, MarketError
from module3.orders import (
    SIDE_BUY,
    SIDES,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    STATUS_PENDING,
    TERMINAL_STATUSES,
    Exit,
    Order,
    OrderError,
    close,
    evaluate,
    expire,
    fill,
    order_from_decision,
    plan_holds_at,
)
from module3.store import OrderStore
from module3.timeutil import format_ist, format_utc, parse_timestamp, utc_now

_HUNDRED: Decimal = Decimal("100")


@dataclass(frozen=True, slots=True)
class PricePoint:
    """The last price this module was told for one stock."""

    price: Decimal
    at: datetime
    source: str
    age_seconds: float | None = None
    #: The price before this one, which is what makes a move across both the
    #: target and the stop detectable at all.
    previous: Decimal | None = None


class OrderTracker:
    """Holds every order, and moves them as prices and the clock arrive."""

    def __init__(
        self,
        *,
        store: OrderStore,
        market: MarketClient | None = None,
        notifier: Notifier | None = None,
        window: TradingWindow | None = None,
        clock: Any = utc_now,
        max_fill_drift_pct: float = 1.0,
        reconcile_max_age_minutes: float = 120.0,
        stale_price_seconds: float = 120.0,
    ) -> None:
        self._store = store
        self._market = market
        self._notifier = notifier or NullNotifier()
        self._window = window or TradingWindow()
        self._clock = clock
        self._max_drift = Decimal(str(max_fill_drift_pct))
        self._reconcile_max_age = float(reconcile_max_age_minutes)
        self._stale = float(stale_price_seconds)
        self._lock = threading.RLock()
        self._orders: dict[str, Order] = store.load()
        self._by_decision: dict[str, str] = {
            order.decision_id: order.order_id for order in self._orders.values()
        }
        self._prices: dict[str, PricePoint] = {}
        self._last_price_error: str | None = None
        self.received = 0
        self.duplicates = 0
        self.fills = 0
        self.exits = 0
        self.expired = 0
        self.pruned = 0

    # -- intake -------------------------------------------------------------- #

    def ingest(self, decision: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Take one decision from Module 2. Idempotent on the decision it came from.

        A decision that is already on the ledger is answered with the order it
        produced rather than a second copy of it. That is what makes the push
        from Module 2 and the pull from Module 2's own log safe to run at the
        same time, and what makes a retry after a timeout harmless.
        """

        moment = now or self._clock()
        payload = dict(decision) if isinstance(decision, Mapping) else {}
        decision_id = str(payload.get("decision_id") or "").strip()
        if decision_id:
            with self._lock:
                known = self._by_decision.get(decision_id)
                if known is not None:
                    self.duplicates += 1
                    return {
                        "stored": False,
                        "duplicate": True,
                        "stood_aside": False,
                        "order": self._orders[known].describe(),
                    }

        if str(payload.get("action") or "").strip().lower() not in SIDES:
            # Standing aside is a real decision and not an order. It is
            # acknowledged, and left where it belongs - in Module 2's log, which
            # is the record of what an agent decided rather than what it held.
            return {
                "stored": False,
                "duplicate": False,
                "stood_aside": True,
                "order": None,
            }

        close_at = self._window.closes_at(moment)
        order = order_from_decision(
            payload,
            now=moment,
            order_id=_new_order_id(),
            session_close_at=close_at,
        )
        with self._lock:
            self._orders[order.order_id] = order
            self._by_decision[order.decision_id] = order.order_id
            self.received += 1
        self._record("order_received", order)

        if moment >= close_at:
            # A decision that arrives after the framework's close is recorded
            # and immediately given up on: entering it would book a position in
            # a market the operator has already stopped trading.
            expired = self._expire(
                order,
                now=moment,
                note=(
                    "this decision arrived after the framework's session window "
                    f"closed ({self._window.close.strftime('%H:%M')} IST), so it "
                    "never became a position"
                ),
            )
            with self._lock:
                return {
                    "stored": True,
                    "duplicate": False,
                    "stood_aside": False,
                    "late": True,
                    "order": expired.describe(),
                }

        # The stream may not have mentioned this stock yet - a quiet stock
        # produces no frames at all - so the order is offered a price at once
        # rather than waiting for one that may not come.
        self._prime(order, now=moment)

        with self._lock:
            return {
                "stored": True,
                "duplicate": False,
                "stood_aside": False,
                "order": self._orders[order.order_id].describe(),
            }

    def reconcile(
        self,
        directory: Any,
        *,
        now: datetime | None = None,
        max_age_minutes: float | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Pick up decisions Module 2 recorded while this module was not listening.

        Only recent ones. A decision from an earlier session belongs to a market
        that has since closed, and booking it now would invent a position at a
        price that no longer exists.
        """

        moment = now or self._clock()
        window = self._reconcile_max_age if max_age_minutes is None else float(max_age_minutes)
        horizon = moment - timedelta(minutes=max(0.0, window))
        considered = 0
        recovered = 0
        skipped = 0
        failed: list[dict[str, str]] = []

        try:
            agent_ids = directory.agents()
        except MarketError as exc:
            return {
                "considered": 0,
                "recovered": 0,
                "skipped_old": 0,
                "failed": [{"error": str(exc)[:200]}],
            }

        for agent_id in agent_ids:
            try:
                decisions = directory.decisions(agent_id, limit=limit)
            except MarketError as exc:
                failed.append({"agent_id": agent_id, "error": str(exc)[:200]})
                continue
            for decision in decisions:
                considered += 1
                created = parse_timestamp(decision.get("created_at")) or parse_timestamp(
                    decision.get("ts")
                )
                if created is None or created < horizon:
                    skipped += 1
                    continue
                try:
                    result = self.ingest(decision, now=moment)
                except OrderError as exc:
                    failed.append(
                        {
                            "decision_id": str(decision.get("decision_id") or ""),
                            "error": str(exc)[:200],
                        }
                    )
                    continue
                if result.get("stored"):
                    recovered += 1
        return {
            "considered": considered,
            "recovered": recovered,
            "skipped_old": skipped,
            "failed": failed,
        }

    # -- prices -------------------------------------------------------------- #

    def watch_symbols(self) -> list[str]:
        """The stocks that still need a price, which is what to subscribe to."""

        with self._lock:
            return sorted(
                {order.symbol for order in self._orders.values() if order.is_live}
            )

    def prices(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """What Module 1 is currently publishing for each watched stock.

        Only the watched ones. A price for a stock nothing is held in is a
        number this module has no use for, and listing it would make the feed
        look busier than the book it is feeding. The age is recomputed here
        rather than echoed from the last frame, so a stream that has gone quiet
        shows as an ageing price rather than a fresh-looking one.
        """

        moment = now or self._clock()
        watched = set(self.watch_symbols())
        with self._lock:
            rows: list[dict[str, Any]] = [
                {
                    "symbol": symbol,
                    "price": float(round(point.price, 2)),
                    "previous": (
                        None
                        if point.previous is None
                        else float(round(point.previous, 2))
                    ),
                    "source": point.source,
                    "price_at": format_utc(point.at),
                    "age_seconds": (
                        round((moment - point.at).total_seconds(), 1)
                        if point.source == "live"
                        else None
                    ),
                }
                for symbol, point in self._prices.items()
                if symbol in watched
            ]
        rows.sort(key=lambda row: row["symbol"])
        return rows

    def on_prices(
        self, rows: Sequence[Mapping[str, Any]], *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Fold a price update in, and move every order it touches."""

        moment = now or self._clock()
        changed: list[Order] = []
        with self._lock:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                symbol = str(row.get("symbol") or "").strip().upper()
                price = row.get("last_price")
                if not symbol or price is None:
                    continue
                previous = self._prices.get(symbol)
                self._prices[symbol] = PricePoint(
                    price=Decimal(str(price)),
                    at=parse_timestamp(row.get("price_at")) or moment,
                    source=str(row.get("price_source") or "none"),
                    age_seconds=_optional_float(row.get("price_age_seconds")),
                    previous=None if previous is None else previous.price,
                )
            for order in list(self._orders.values()):
                if not order.is_live:
                    continue
                moved = self._advance(order, now=moment)
                if moved is not None:
                    changed.append(moved)
        return [order.describe() for order in changed]

    def refresh_prices(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Ask Module 1 outright, as a safety net under the stream."""

        moment = now or self._clock()
        symbols = self.watch_symbols()
        if not symbols or self._market is None:
            return {"symbols": 0, "rows": 0, "unavailable": []}
        payload = self._market.prices(symbols)
        rows = [row for row in (payload.get("rows") or []) if isinstance(row, Mapping)]
        self.on_prices(rows, now=moment)
        return {
            "symbols": len(symbols),
            "rows": len(rows),
            "unavailable": list(payload.get("unavailable") or []),
        }

    # -- the clock ----------------------------------------------------------- #

    def sweep(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Move every order the passage of time alone can move.

        A time exit does not wait for a tick: a stock that has stopped trading
        still has a deadline, and an order nobody can see still has to be
        answered for.
        """

        moment = now or self._clock()
        changed: list[Order] = []
        with self._lock:
            for order in list(self._orders.values()):
                if not order.is_live:
                    continue
                if order.status == STATUS_PENDING and self._waited_too_long(
                    order, now=moment
                ):
                    changed.append(
                        self._expire(
                            order,
                            now=moment,
                            note=(
                                "no live price arrived inside the holding window this "
                                "order was given, so it never became a position"
                            ),
                        )
                    )
                    continue
                moved = self._advance(order, now=moment)
                if moved is not None:
                    changed.append(moved)
        return [order.describe() for order in changed]

    # -- reads --------------------------------------------------------------- #

    def orders(self) -> list[Order]:
        with self._lock:
            return list(self._orders.values())

    def order(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    # -- the daily book ------------------------------------------------------ #

    def summary(
        self, *, agent_id: str | None = None, day: str | None = None
    ) -> dict[str, Any]:
        """How a period went, in the few numbers anybody actually asks for.

        "How am I doing" is not answered by a list to add up. This is the daily
        book in one answer: what is open, what has closed, what it made, and the
        open positions themselves - because the agent that wakes up next needs
        to know what it already holds before it decides to hold more.
        """

        with self._lock:
            rows = [
                order
                for order in self._orders.values()
                if (agent_id is None or order.agent_id == agent_id)
                and (day is None or order.session_date == day)
            ]
            return {
                **self._totals(rows),
                "agent_id": agent_id,
                "day": day,
                "open_positions": [_position(order) for order in rows if order.is_live],
            }

    def days(self) -> list[dict[str, Any]]:
        """Every exchange day the book holds anything for, newest first."""

        with self._lock:
            found = sorted(
                {
                    order.session_date
                    for order in self._orders.values()
                    if order.session_date
                },
                reverse=True,
            )
            by_day = {
                day: [order for order in self._orders.values() if order.session_date == day]
                for day in found
            }
        return [{"day": day, **self._totals(by_day[day])} for day in found]

    def prune(self, *, before: str) -> dict[str, Any]:
        """Forget finished orders taken before a day, and keep everything since.

        This is what makes Module 3 the book of the day rather than a second
        archive: the long history belongs to Module 4, and keeping a copy of it
        here is how the two come to disagree. Pass tomorrow's date to clear the
        book outright.

        Two things it will not do. It will not touch an order that is still
        open - a position being watched is not history, whatever day it was
        taken on - and it will not guess at an order whose day it cannot work
        out, because "when did this happen" is not a question to answer by
        default. It also cannot know whether Module 4 has the orders yet: that
        is the caller's business, so the answer reports exactly what was
        dropped rather than reassuring anybody.
        """

        with self._lock:
            doomed = [
                order
                for order in self._orders.values()
                if order.status in TERMINAL_STATUSES
                and order.session_date is not None
                and order.session_date < before
            ]
            holding = sorted(
                order.symbol
                for order in self._orders.values()
                if order.is_live
                and order.session_date is not None
                and order.session_date < before
            )
            undated = sorted(
                order.order_id
                for order in self._orders.values()
                if order.session_date is None
            )
            if not doomed:
                return {
                    "before": before,
                    "removed": 0,
                    "kept": len(self._orders),
                    "still_holding": holding,
                    "undated": undated,
                }
            forget = {order.order_id for order in doomed}
            for order in doomed:
                self._orders.pop(order.order_id, None)
                self._by_decision.pop(order.decision_id, None)
            report = {
                "before": before,
                "removed": len(doomed),
                "kept": len(self._orders),
                "symbols": sorted({order.symbol for order in doomed}),
                "realised_forgotten": float(
                    round(
                        sum(
                            (order.pnl.gross for order in doomed if order.pnl),
                            Decimal("0"),
                        ),
                        2,
                    )
                ),
                "still_holding": holding,
                "undated": undated,
                "note": (
                    "Module 3 has forgotten these; the archive is their record now"
                ),
            }
        self._store.write_checkpoint(self._orders)
        self._store.rewrite_events(
            forget,
            note={
                "kind": "pruned",
                "before": before,
                "removed": report["removed"],
                "kept": report["kept"],
            },
        )
        self.pruned += report["removed"]
        return report

    def _totals(self, rows: Sequence[Order]) -> dict[str, Any]:
        """The arithmetic a day's book is judged by."""

        closed = [order for order in rows if order.status == STATUS_CLOSED and order.pnl]
        realised = sum(
            (order.pnl.gross for order in closed if order.pnl is not None), Decimal("0")
        )
        wins = [order for order in closed if order.pnl is not None and order.pnl.gross > 0]
        losses = [order for order in closed if order.pnl is not None and order.pnl.gross < 0]
        multiples = [
            order.pnl.r_multiple
            for order in closed
            if order.pnl is not None and order.pnl.r_multiple is not None
        ]
        results = [
            (order.pnl.gross, order.symbol)
            for order in closed
            if order.pnl is not None
        ]
        return {
            "orders": len(rows),
            "open": sum(1 for order in rows if order.is_live),
            "closed": len(closed),
            "expired": sum(1 for order in rows if order.status == STATUS_EXPIRED),
            "realised": float(round(realised, 2)),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (
                None if not closed else round(len(wins) / len(closed) * 100, 1)
            ),
            "average_r": (
                None
                if not multiples
                else float(round(sum(multiples) / len(multiples), 2))
            ),
            "best": (
                None
                if not results
                else {"symbol": max(results)[1], "gross": float(round(max(results)[0], 2))}
            ),
            "worst": (
                None
                if not results
                else {"symbol": min(results)[1], "gross": float(round(min(results)[0], 2))}
            ),
        }

    def view(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Every order, with the live price and the mark-to-market on top of it."""

        moment = now or self._clock()
        with self._lock:
            rows: list[dict[str, Any]] = []
            for order in self._orders.values():
                row = order.describe()
                point = self._prices.get(order.symbol)
                row["last_price"] = None if point is None else float(round(point.price, 2))
                row["last_price_at"] = None if point is None else format_utc(point.at)
                row["last_price_source"] = None if point is None else point.source
                row["price_age_seconds"] = (
                    round((moment - point.at).total_seconds(), 1)
                    if point is not None and point.source == "live"
                    else None
                )
                row["unrealised_pnl"] = (
                    float(order.pnl_at(point.price))
                    if point is not None and order.is_open
                    else None
                )
                row["deadline_seconds"] = (
                    None
                    if order.deadline is None
                    else round((order.deadline - moment).total_seconds(), 1)
                )
                rows.append(row)
        rows.sort(key=lambda row: (str(row.get("received_at") or ""), row["order_id"]), reverse=True)
        return rows

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_status: dict[str, int] = {}
            for order in self._orders.values():
                by_status[order.status] = by_status.get(order.status, 0) + 1
            return {
                "orders": len(self._orders),
                "by_status": by_status,
                "received": self.received,
                "duplicates": self.duplicates,
                "fills": self.fills,
                "exits": self.exits,
                "expired": self.expired,
                "prices": len(self._prices),
                "watching": len(self.watch_symbols()),
                "last_price_error": self._last_price_error,
            }

    # -- internals ----------------------------------------------------------- #

    def _advance(self, order: Order, *, now: datetime) -> Order | None:
        """Move one order as far as its price and the clock allow."""

        original = order
        if order.status == STATUS_PENDING:
            order = self._fill_if_live(order, now=now)
        if order.status == STATUS_OPEN:
            exit_ = self._exit_for(order, now=now)
            if exit_ is not None:
                order = self._close(order, exit_, now=now)
        return None if order is original else order

    def _fill_if_live(self, order: Order, *, now: datetime) -> Order:
        """Execute the order, at the first live price Module 1 publishes for it.

        The price the position is entered at is the market's, not the decision's,
        so it becomes the order's entry and everything after it is measured from
        there. Two rules stop that becoming a licence to enter anywhere:

        * the plan must still bracket the market - once the price is at or past
          the target or the stop there is no trade left to take; and
        * the plan must not be stale - past the operator's tolerance these are
          no longer the levels that were decided on.
        """

        point = self._prices.get(order.symbol)
        if point is None or not self._is_live(point):
            return order

        if not plan_holds_at(order, point.price):
            passed, limit = _level_passed(order, point.price)
            return self._expire(
                order,
                now=now,
                note=(
                    f"the first live price, {_price_text(point.price)}, is already "
                    f"past this plan's {passed} at {_price_text(limit)}, so there is "
                    "no trade left to take"
                ),
            )

        planned = order.decision_entry_price or order.entry_price
        drift = abs(point.price - planned) / planned * _HUNDRED
        if drift > self._max_drift:
            return self._expire(
                order,
                now=now,
                note=(
                    f"the market moved {float(drift):.2f}% from the {planned} this "
                    "plan was built on before a price could be taken, so these are "
                    "no longer the levels that were decided on"
                ),
            )

        opened = fill(
            order, at=now, price=point.price, drift_pct=float(round(drift, 4))
        )
        with self._lock:
            self._orders[order.order_id] = opened
            self.fills += 1
        self._record("order_filled", opened)
        return opened

    def _exit_for(self, order: Order, *, now: datetime) -> Exit | None:
        point = self._prices.get(order.symbol)
        if point is None:
            return None
        return evaluate(
            order,
            price=point.price,
            at=point.at,
            now=now,
            previous_price=point.previous,
        )

    def _close(self, order: Order, exit_: Exit, *, now: datetime) -> Order:
        closed = close(order, exit_, at=now)
        with self._lock:
            self._orders[order.order_id] = closed
            self.exits += 1
        self._record("order_closed", closed)
        return closed

    def _expire(self, order: Order, *, now: datetime, note: str) -> Order:
        expired = expire(order, at=now, note=note)
        with self._lock:
            self._orders[order.order_id] = expired
            self.expired += 1
        self._record("order_expired", expired)
        return expired

    def _waited_too_long(self, order: Order, *, now: datetime) -> bool:
        if order.received_at is None:
            return False
        waiting = order.received_at + timedelta(minutes=order.max_hold_minutes)
        if order.session_close_at is not None:
            waiting = min(waiting, order.session_close_at)
        return now >= waiting

    def _is_live(self, point: PricePoint) -> bool:
        """Whether a price is one a position could actually have been taken at.

        Module 1 already downgrades a tick older than its own freshness window
        to a stored price, so the source alone nearly settles it. The age is
        checked anyway: this is the module that acts on the answer, and a
        boundary that trusts its caller is not a boundary.
        """

        if point.source != "live":
            return False
        return not (point.age_seconds is not None and point.age_seconds > self._stale)

    def _prime(self, order: Order, *, now: datetime) -> None:
        """Offer the new order a price straight away, if Module 1 will answer."""

        if self._market is None:
            return
        try:
            payload = self._market.prices([order.symbol])
        except MarketError as exc:
            with self._lock:
                self._last_price_error = str(exc)[:200]
            return
        rows = [row for row in (payload.get("rows") or []) if isinstance(row, Mapping)]
        if rows:
            self.on_prices(rows, now=now)

    def _record(self, kind: str, order: Order) -> None:
        """Persist a change: the log is the history, the checkpoint the state."""

        self._store.append_event(kind, order)
        self._store.write_checkpoint(self._orders)
        self._notify_order(kind, order)

    def _notify_order(self, kind: str, order: Order) -> None:
        """Say what happened to one order, in the terms a trade is read in.

        Every transition is reported because every transition changes what the
        operator holds: a fill is a position, a close is money, an expiry is a
        trade that never happened. The message names the order's own numbers.
        """

        icon = {
            "order_received": "📥",
            "order_filled": "✅",
            "order_closed": "🏁",
            "order_expired": "⌛",
        }.get(kind, "•")
        title = {
            "order_received": "New order",
            "order_filled": "Filled",
            "order_closed": "Closed",
            "order_expired": "Expired",
        }.get(kind, kind)
        lines = [
            f"{icon} <b>{title}</b> · {escape(order.agent_id)} · "
            f"{escape(order.side.upper())} {order.quantity} {escape(order.symbol)}",
        ]
        if kind == "order_received":
            lines.append(
                f"planned entry {_price_text(order.decision_entry_price)} · "
                f"stop {_price_text(order.stop_loss)} · "
                f"target {_price_text(order.target)} · "
                f"hold {order.max_hold_minutes}m"
            )
        elif kind == "order_filled":
            drift = (
                "n/a"
                if order.fill_drift_pct is None
                else f"{order.fill_drift_pct:.2f}%"
            )
            lines.append(
                f"entry {_price_text(order.entry_price)} "
                f"(planned {_price_text(order.decision_entry_price)}, drift {drift}) · "
                f"stop {_price_text(order.stop_loss)} · "
                f"target {_price_text(order.target)}"
            )
            if order.deadline is not None:
                lines.append(f"deadline {format_ist(order.deadline)} IST")
        elif kind == "order_closed":
            lines.append(
                f"exit {escape(order.exit_reason)} @ {_price_text(order.exit_price)}"
            )
            if order.pnl is not None:
                settled = order.pnl
                multiple = (
                    "n/a"
                    if settled.r_multiple is None
                    else f"{settled.r_multiple:+.2f}R"
                )
                lines.append(
                    f"P&L {settled.gross:+.2f} ({settled.percent:+.2f}%) · "
                    f"{multiple} · {escape(settled.outcome)}"
                )
            if order.ambiguous_exit:
                lines.append(
                    "⚠️ one price cleared both the target and the stop; the stop is "
                    "the outcome that counts"
                )
            if order.exit_note:
                lines.append(escape(order.exit_note))
        elif order.exit_note:
            lines.append(escape(order.exit_note))
        self._notifier.notify(
            "\n".join(lines),
            key=f"module3.{kind}.{order.order_id}",
            dedupe_seconds=0.0,
        )


def _new_order_id() -> str:
    return f"ord_{uuid4().hex[:12]}"


def _level_passed(order: Order, price: Decimal) -> tuple[str, Decimal]:
    """Which of the plan's levels the market has already reached."""

    beyond_target = (
        price >= order.target if order.side == SIDE_BUY else price <= order.target
    )
    if beyond_target:
        return "target", order.target
    return "stop", order.stop_loss


def _price_text(value: Decimal) -> str:
    """A price as a person reads it, in the two places ticks are quoted in."""

    return format(value, ".2f")


def _position(order: Order) -> dict[str, Any]:
    """An open position, short enough to sit in a model's context.

    The full record is a page of JSON; what decides whether to add to a
    position is the side, the size, the levels, the price now and what it is
    making. The rest is a lookup away for anyone who wants it.
    """

    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "entry_price": _number(order.entry_price),
        "target": _number(order.target),
        "stop_loss": _number(order.stop_loss),
        "status": order.status,
        "deadline": None if order.deadline is None else format_utc(order.deadline),
    }


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def _optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

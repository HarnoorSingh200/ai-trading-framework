"""Answers drawn from the long order record.

The archive holds facts. This module turns those facts into the few summaries a
person actually uses to judge an agent: what it made, how often it was right,
what a typical trade returned in units of risk, how deep the run went between
peaks, and where the results came from.

Everything is calculated from the same rows and the same :func:`totals`
function. The dashboard, the per-agent table and the per-day table must not be
able to disagree about what a rupee or an R means.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from module4.timeutil import format_utc, parse_timestamp, utc_now

EXIT_LABELS = {
    "target": "Target",
    "stop_loss": "Stop",
    "timeout": "Time cap",
    "trailing_stop": "Trailing stop",
    "session_close": "Session close",
}

SIDE_LABELS = {"buy": "Long", "sell": "Short"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _gross(row: Mapping[str, Any]) -> float:
    pnl = row.get("pnl")
    return _number(pnl.get("gross")) if isinstance(pnl, Mapping) else 0.0


def _r_multiple(row: Mapping[str, Any]) -> float | None:
    pnl = row.get("pnl")
    if not isinstance(pnl, Mapping) or pnl.get("r_multiple") is None:
        return None
    return _number(pnl.get("r_multiple"))


def _risk(row: Mapping[str, Any]) -> float | None:
    pnl = row.get("pnl")
    value = pnl.get("risk") if isinstance(pnl, Mapping) else row.get("risk")
    if value is None:
        return None
    return _number(value)


def _closed(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if row.get("status") == "closed" and isinstance(row.get("pnl"), Mapping)
    ]


def _moment(row: Mapping[str, Any]) -> datetime | None:
    for field in ("ended_at", "filled_at", "received_at"):
        moment = parse_timestamp(row.get(field) or "")
        if moment is not None:
            return moment
    return None


def _ordered_closed(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    closed = _closed(rows)
    return sorted(
        closed,
        key=lambda row: (
            _moment(row) is None,
            _moment(row) or datetime.min,
            str(row.get("order_id") or ""),
        ),
    )


def _hold_minutes(row: Mapping[str, Any]) -> float | None:
    started = parse_timestamp(row.get("filled_at") or row.get("received_at") or "")
    ended = parse_timestamp(row.get("ended_at") or "")
    if started is None or ended is None or ended < started:
        return None
    return (ended - started).total_seconds() / 60.0


def totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What a set of orders came to.

    Every number a person would otherwise add up by hand, and the few they
    could not add up at all: profit factor, average win and loss, risk carried,
    and the average result in units of that risk. Undefined ratios stay
    ``None`` rather than becoming a convenient zero.
    """

    closed = _closed(rows)
    gross = [_gross(row) for row in closed]
    multiples = [
        value for row in closed if (value := _r_multiple(row)) is not None
    ]
    risks = [value for row in closed if (value := _risk(row)) is not None]
    holds = [value for row in closed if (value := _hold_minutes(row)) is not None]
    wins = [value for value in gross if value > 0]
    losses = [value for value in gross if value < 0]
    best = max(closed, key=_gross, default=None)
    worst = min(closed, key=_gross, default=None)
    average_win = sum(wins) / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None
    return {
        "orders": len(rows),
        "open": sum(1 for row in rows if row.get("status") in {"open", "pending"}),
        "closed": len(closed),
        "expired": sum(1 for row in rows if row.get("status") == "expired"),
        "realised": round(sum(gross), 2),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(closed) - len(wins) - len(losses),
        "win_rate": None if not closed else round(len(wins) / len(closed) * 100, 1),
        "average_gross": None if not closed else round(sum(gross) / len(closed), 2),
        "average_r": (
            None if not multiples else round(sum(multiples) / len(multiples), 2)
        ),
        "average_win": None if average_win is None else round(average_win, 2),
        "average_loss": None if average_loss is None else round(average_loss, 2),
        "profit_factor": (
            None if not losses else round(sum(wins) / abs(sum(losses)), 2)
        ),
        "average_risk": None if not risks else round(sum(risks) / len(risks), 2),
        "total_risk": round(sum(risks), 2),
        "average_hold_minutes": (
            None if not holds else round(sum(holds) / len(holds), 1)
        ),
        "r_samples": len(multiples),
        "best": None
        if best is None
        else {"symbol": best.get("symbol"), "gross": round(_gross(best), 2)},
        "worst": None
        if worst is None
        else {"symbol": worst.get("symbol"), "gross": round(_gross(worst), 2)},
    }


def _streak(closed: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    outcomes: list[tuple[str, float]] = []
    for row in closed:
        gross = _gross(row)
        outcome = "win" if gross > 0 else "loss" if gross < 0 else "flat"
        outcomes.append((outcome, gross))
    if not outcomes:
        return None
    last = outcomes[-1][0]
    count = 0
    for outcome, _ in reversed(outcomes):
        if outcome != last:
            break
        count += 1
    return {"outcome": last, "count": count}


def _longest_streak(closed: Sequence[Mapping[str, Any]], wanted: str) -> int:
    longest = current = 0
    for row in closed:
        gross = _gross(row)
        outcome = "win" if gross > 0 else "loss" if gross < 0 else "flat"
        if outcome == wanted:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _point(row: Mapping[str, Any], cumulative: float) -> dict[str, Any] | None:
    moment = _moment(row)
    if moment is None:
        return None
    return {
        "at": format_utc(moment),
        "day": row.get("session_date"),
        "agent_id": row.get("agent_id"),
        "order_id": row.get("order_id"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "status": row.get("status"),
        "exit_reason": row.get("exit_reason"),
        "r_multiple": _r_multiple(row),
        "gross": round(_gross(row), 2),
        "cumulative": round(cumulative, 2),
    }


def _curve(closed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    running = 0.0
    for row in closed:
        running += _gross(row)
        point = _point(row, running)
        if point is not None:
            points.append(point)
    return points


def _drawdown(points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Peak-to-trough decline in cumulative *realised* P&L.

    This is deliberately not called account drawdown: Module 4 knows what the
    closed trades made, not the account's cash balance. Starting from zero is
    the only starting value the record can defend.
    """

    peak = 0.0
    peak_point: Mapping[str, Any] | None = None
    worst = 0.0
    start = None
    trough = None
    for point in points:
        value = float(point.get("cumulative") or 0.0)
        if value >= peak:
            peak = value
            peak_point = point
        decline = peak - value
        if decline > worst:
            worst = decline
            start = peak_point
            trough = point
    return {
        "gross": round(worst, 2),
        "peak": round(peak, 2),
        "from": None if start is None else start.get("at"),
        "to": None if trough is None else trough.get("at"),
        "recovered": bool(points)
        and float(points[-1].get("cumulative") or 0.0) >= peak,
    }


def _agent_rows(
    rows: Sequence[Mapping[str, Any]],
    curves: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        agent_id = str(row.get("agent_id") or "unassigned")
        groups[agent_id].append(row)
    found: list[dict[str, Any]] = []
    for agent_id, group in groups.items():
        numbers = totals(group)
        curve = list(curves.get(agent_id, ()))
        day_gross: dict[str, float] = {
            str(row["session_date"]): 0.0
            for row in group
            if row.get("session_date")
        }
        for row in _closed(group):
            day = row.get("session_date")
            if day:
                day_gross[str(day)] += _gross(row)
        green_days = sum(1 for value in day_gross.values() if value > 0)
        red_days = sum(1 for value in day_gross.values() if value < 0)
        found.append(
            {
                "agent_id": agent_id,
                **numbers,
                "trading_days": len(day_gross),
                "green_days": green_days,
                "red_days": red_days,
                "flat_days": len(day_gross) - green_days - red_days,
                "max_drawdown": _drawdown(curve)["gross"],
                "final_cumulative": (
                    None if not curve else float(curve[-1].get("cumulative") or 0.0)
                ),
            }
        )
    found.sort(key=lambda row: (-(row["realised"] or 0.0), row["agent_id"]))
    return found


def _day_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        day = row.get("session_date")
        if day:
            groups[str(day)].append(row)
    found: list[dict[str, Any]] = []
    for day in sorted(groups, reverse=True):
        group = groups[day]
        by_agent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in group:
            by_agent[str(row.get("agent_id") or "unassigned")].append(row)
        agents = [
            {
                "agent_id": agent_id,
                **totals(agent_rows),
            }
            for agent_id, agent_rows in sorted(by_agent.items())
        ]
        found.append({"day": day, **totals(group), "agents": agents})
    return found


def _r_bucket(row: Mapping[str, Any]) -> str | None:
    value = _r_multiple(row)
    if value is None:
        return None
    if value <= -2:
        return "le_2"
    if value < -1:
        return "m2_m1"
    if value < 0:
        return "m1_0"
    if value == 0:
        return "flat"
    if value < 1:
        return "0_1"
    if value < 2:
        return "1_2"
    return "ge_2"


def _r_distribution(closed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    bins = [
        ("le_2", "≤ −2R"),
        ("m2_m1", "−2 to −1R"),
        ("m1_0", "−1 to 0R"),
        ("flat", "0R"),
        ("0_1", "0 to 1R"),
        ("1_2", "1 to 2R"),
        ("ge_2", "≥ 2R"),
    ]
    counts = {key: 0 for key, _ in bins}
    gross = {key: 0.0 for key, _ in bins}
    for row in closed:
        key = _r_bucket(row)
        if key is None:
            continue
        counts[key] += 1
        gross[key] += _gross(row)
    return [
        {
            "key": key,
            "label": label,
            "count": counts[key],
            "gross": round(gross[key], 2),
        }
        for key, label in bins
    ]


def _exit_rows(closed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in closed:
        reason = str(row.get("exit_reason") or "not_recorded")
        groups[reason].append(row)
    found = [
        {
            "reason": reason,
            "label": EXIT_LABELS.get(reason, reason.replace("_", " ").title()),
            "count": len(rows),
            "gross": round(sum(_gross(row) for row in rows), 2),
        }
        for reason, rows in groups.items()
    ]
    found.sort(key=lambda row: (-row["count"], row["label"]))
    return found


def _side_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        side = str(row.get("side") or "unknown")
        groups[side].append(row)
    return [
        {
            "side": side,
            "label": SIDE_LABELS.get(side, side.title()),
            **totals(group),
        }
        for side, group in sorted(groups.items())
    ]


def _symbol_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol") or "Unknown")
        groups[symbol].append(row)
    found = [
        {"symbol": symbol, **totals(group)}
        for symbol, group in groups.items()
    ]
    found.sort(key=lambda row: (-(row["realised"] or 0.0), row["symbol"]))
    return found


def build_analytics(
    rows: Sequence[Mapping[str, Any]],
    *,
    agent_id: str | None = None,
    day: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The dashboard's one analytics answer for a scope of archived orders."""

    visible = [
        row
        for row in rows
        if (not agent_id or row.get("agent_id") == agent_id)
        and (not day or row.get("session_date") == day)
    ]
    agent_scoped = [
        row
        for row in rows
        if not agent_id or row.get("agent_id") == agent_id
    ]
    closed = _ordered_closed(visible)
    all_curves: dict[str, list[dict[str, Any]]] = {}
    by_agent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in closed:
        by_agent[str(row.get("agent_id") or "unassigned")].append(row)
    for agent, agent_rows in by_agent.items():
        all_curves[agent] = _curve(agent_rows)

    equity = _curve(closed)
    positions = {
        str(point.get("order_id")): index for index, point in enumerate(equity)
    }
    for index, point in enumerate(equity):
        point["trade_index"] = index
    for curve in all_curves.values():
        for point in curve:
            point["trade_index"] = positions.get(str(point.get("order_id")))
    agent_rows = _agent_rows(visible, all_curves)
    days = _day_rows(visible)
    drawdown = _drawdown(equity)
    green_days = sum(1 for row in days if float(row["realised"] or 0.0) > 0)
    red_days = sum(1 for row in days if float(row["realised"] or 0.0) < 0)
    flat_days = len(days) - green_days - red_days
    summary = {
        **totals(visible),
        "trading_days": len(days),
        "green_days": green_days,
        "red_days": red_days,
        "flat_days": flat_days,
        "day_win_rate": None if not days else round(green_days / len(days) * 100, 1),
        "max_drawdown": drawdown["gross"],
        "drawdown": drawdown,
        "current_streak": _streak(closed),
        "longest_win_streak": _longest_streak(closed, "win"),
        "longest_loss_streak": _longest_streak(closed, "loss"),
        "first_day": min((str(row["day"]) for row in days), default=None),
        "last_day": max((str(row["day"]) for row in days), default=None),
    }
    return {
        "generated_at": format_utc(now or utc_now()),
        "filters": {"agent_id": agent_id, "day": day},
        "basis": "gross realised P&L before fees and taxes; R is measured at entry",
        "summary": summary,
        "agents": agent_rows,
        "days": days,
        "context_days": _day_rows(agent_scoped) if day else days,
        "equity": equity,
        "agent_curves": [
            {
                "agent_id": row["agent_id"],
                "final_cumulative": row["final_cumulative"],
                "max_drawdown": row["max_drawdown"],
                "points": all_curves.get(row["agent_id"], []),
            }
            for row in agent_rows
        ],
        "r_distribution": _r_distribution(closed),
        "exits": _exit_rows(closed),
        "sides": _side_rows(visible),
        "symbols": _symbol_rows(visible),
    }

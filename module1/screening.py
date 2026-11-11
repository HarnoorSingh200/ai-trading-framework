"""Filtering the universe by its own published numbers.

This is the vocabulary and the arithmetic behind ``/v1/market/screen``. It
exists so an agent can ask a question about the whole universe - "which stocks
are oversold on volume" - and get back three rows instead of fifty rows of
forty-one fields. The saving is not at the broker, which already holds
everything in memory; it is in what crosses the boundary into a model's
context, and in the shortlist being reproducible afterwards.

Three rules, each of which is a bug if it is relaxed:

* **A field that is not in the catalogue cannot be filtered on.** The catalogue
  is built from :data:`module1.indicators.FIELDS` plus the closed bar's own
  numbers, so a filter can never name something the module does not publish.
* **A ``null`` never matches.** ``null`` means "not enough history yet"; an RSI
  that is null because a symbol is new is not an RSI of zero, and comparing it
  as one would quietly select exactly the wrong stocks.
* **What is filtered on is what is shown.** Values are compared exactly as
  ``/v1/market/screen`` returns them, so a row that matched is a row whose
  printed value satisfies the condition the agent wrote.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Final

from module1.indicators import FIELDS, GROUP_ORDER

#: How many rows a screen returns when it is not told.
DEFAULT_LIMIT: Final[int] = 5
#: The most rows a single screen will ever hand back. A shortlist is the point:
#: a screen that returns the universe has not screened anything.
MAX_LIMIT: Final[int] = 25

NUMERIC_OPS: Final[tuple[str, ...]] = ("<", "<=", ">", ">=", "==", "!=")
ORDERING_OPS: Final[tuple[str, ...]] = ("<", "<=", ">", ">=")
EQUALITY_OPS: Final[tuple[str, ...]] = ("==", "!=")

MATCH_ALL: Final[str] = "all"
MATCH_ANY: Final[str] = "any"
MATCHES: Final[tuple[str, ...]] = (MATCH_ALL, MATCH_ANY)

SOURCE_BAR: Final[str] = "bar"
SOURCE_DAY: Final[str] = "day"
SOURCE_INDICATOR: Final[str] = "indicator"

#: Units that are ordered numbers.
_NUMERIC_UNITS: Final[frozenset[str]] = frozenset(
    {"price", "percent", "number", "ratio", "volume"}
)

#: Fields that identify a row rather than describe it. They are always present
#: in the answer and can never be filtered or ranked on: a screen answers "which
#: stocks match" rather than "find one named stock", which is what the symbol
#: bundle is for.
IDENTITY_FIELDS: Final[tuple[str, ...]] = ("symbol", "name")


class ScreenError(ValueError):
    """A filter that cannot be honoured, with the reason it cannot."""

    def __init__(self, message: str, *, field: str | None = None, op: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.op = op


@dataclass(frozen=True, slots=True)
class FilterField:
    """One thing a filter may name."""

    name: str
    label: str
    group: str
    unit: str
    ops: tuple[str, ...]
    source: str

    @property
    def numeric(self) -> bool:
        return self.unit in _NUMERIC_UNITS

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "group": self.group,
            "unit": self.unit,
            "ops": list(self.ops),
            "source": self.source,
        }


def _ops_for(unit: str) -> tuple[str, ...]:
    if unit in _NUMERIC_UNITS:
        return NUMERIC_OPS
    return EQUALITY_OPS


#: The closed bar's own numbers, which travel beside every indicator value.
_BAR_FIELDS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("open", "Open", "bar", "price"),
    ("high", "High", "bar", "price"),
    ("low", "Low", "bar", "price"),
    ("close", "Close", "bar", "price"),
    ("change", "Change", "bar", "price"),
    ("change_percent", "Change %", "bar", "percent"),
    ("move_percent", "Move %", "bar", "percent"),
    ("range_percent", "Range %", "bar", "percent"),
    ("volume", "Volume", "bar", "volume"),
    ("volume_ratio", "Volume ×", "bar", "ratio"),
    ("minutes_present", "Minutes present", "bar", "number"),
    ("minutes_expected", "Minutes expected", "bar", "number"),
    ("bar_age_seconds", "Bar age (s)", "bar", "number"),
    ("is_complete", "Complete", "bar", "bool"),
    ("is_gappy", "Short of minutes", "bar", "bool"),
)

#: The day's own numbers, as the board row carries them.
_DAY_FIELDS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("day_close", "Day close", "day", "price"),
    ("day_change_percent", "Day %", "day", "percent"),
    ("day_volume", "Day volume", "day", "volume"),
    ("day_low", "Day low", "day", "price"),
    ("day_high", "Day high", "day", "price"),
)

#: What the stock *is*, as opposed to how it traded. Filtering by industry is a
#: real question - "which IT names are moving" - so it is filterable text.
_CONTEXT_FIELDS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("industry", "Industry", "security", "text"),
)


def _build_fields() -> tuple[FilterField, ...]:
    """The catalogue: the bar, the day, then every indicator value."""

    built: list[FilterField] = []
    for name, label, group, unit in (*_BAR_FIELDS, *_DAY_FIELDS, *_CONTEXT_FIELDS):
        built.append(
            FilterField(name=name, label=label, group=group, unit=unit, ops=_ops_for(unit), source=group)
        )
    for spec in FIELDS:
        built.append(
            FilterField(
                name=spec.name,
                label=spec.label,
                group=spec.group,
                unit=spec.unit,
                ops=_ops_for(spec.unit),
                source=SOURCE_INDICATOR,
            )
        )
    return tuple(built)


SCREEN_FIELDS: Final[tuple[FilterField, ...]] = _build_fields()

_BY_NAME: Final[dict[str, FilterField]] = {field.name: field for field in SCREEN_FIELDS}

#: The order groups are shown in: the bar and the day first, then the indicator
#: groups in the order the table uses.
_GROUP_ORDER: Final[tuple[tuple[str, str], ...]] = (
    ("bar", "This candle"),
    ("day", "The day"),
    ("security", "The stock"),
    *tuple((key, label) for key, label, _default in GROUP_ORDER),
)


def field_for(name: Any) -> FilterField:
    """The filterable field called ``name``, or a clear refusal."""

    key = str(name or "").strip()
    if key in IDENTITY_FIELDS:
        raise ScreenError(
            f"{key} identifies a row rather than describing it, so it cannot be filtered on",
            field=key,
        )
    found = _BY_NAME.get(key)
    if found is None:
        # The caller is usually a model that guessed. Naming the nearest few
        # costs nothing here and saves it a whole round over there - and a round
        # is a model call, which is the most expensive thing in the loop.
        near = get_close_matches(key, list(_BY_NAME), n=3, cutoff=0.6)
        hint = f"; the closest are {', '.join(near)}" if near else ""
        raise ScreenError(
            f"{key!r} is not a field this module publishes{hint}", field=key
        )
    return found


def _value_of(row: Mapping[str, Any], field: FilterField) -> Any:
    """Read a field from a row, treating absence as null."""

    values = row.get("indicators")
    if isinstance(values, Mapping) and field.name in values:
        return values[field.name]
    return row.get(field.name)


def _check_value(field: FilterField, value: Any, op: str) -> Any:
    if field.unit == "bool":
        if not isinstance(value, bool):
            raise ScreenError(
                f"{field.name} is a yes/no field, so it compares against true or false",
                field=field.name,
                op=op,
            )
        return value
    if field.unit == "text":
        if not isinstance(value, str):
            raise ScreenError(
                f"{field.name} is text, so it compares against a string", field=field.name, op=op
            )
        return value
    # ``bool`` is a subclass of ``int``: a flag is not a price.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScreenError(
            f"{field.name} is a number, so it compares against a number", field=field.name, op=op
        )
    return float(value)


@dataclass(frozen=True, slots=True)
class Condition:
    """One comparison, already checked against the catalogue."""

    field: FilterField
    op: str
    value: Any

    def describe(self) -> dict[str, Any]:
        return {"field": self.field.name, "op": self.op, "value": self.value}

    def matches(self, row: Mapping[str, Any]) -> bool:
        found = _value_of(row, self.field)
        # Null is "not enough history yet". It satisfies nothing - not even
        # ``!=``, which would otherwise select every symbol with no data.
        if found is None:
            return False
        if self.field.numeric:
            if isinstance(found, bool) or not isinstance(found, (int, float)):
                return False
            left, right = float(found), float(self.value)
        else:
            left, right = found, self.value
        if self.op == "<":
            return left < right
        if self.op == "<=":
            return left <= right
        if self.op == ">":
            return left > right
        if self.op == ">=":
            return left >= right
        if self.op == "==":
            return left == right
        return left != right


def parse_conditions(where: Any) -> tuple[Condition, ...]:
    """Turn the filter the caller sent into conditions, or refuse it."""

    if where is None:
        return ()
    if not isinstance(where, (list, tuple)):
        raise ScreenError("where must be a list of {field, op, value} conditions")
    conditions: list[Condition] = []
    for item in where:
        if not isinstance(item, Mapping):
            raise ScreenError("every condition must be an object with field, op and value")
        field = field_for(item.get("field"))
        op = str(item.get("op") or "").strip()
        if op not in field.ops:
            raise ScreenError(
                f"{field.name} takes {', '.join(field.ops)}, not {op or 'no operator'!r}",
                field=field.name,
                op=op,
            )
        if "value" not in item:
            raise ScreenError(f"the condition on {field.name} has no value", field=field.name, op=op)
        conditions.append(
            Condition(field=field, op=op, value=_check_value(field, item["value"], op))
        )
    return tuple(conditions)


def _rank_key(row: Mapping[str, Any], field: FilterField, direction: str) -> tuple[Any, ...]:
    """Biggest first for ``desc``. A null sorts last, whichever way that is."""

    found = _value_of(row, field)
    usable = (
        found is not None
        and not isinstance(found, bool)
        and isinstance(found, (int, float))
    )
    value = float(found) if usable else 0.0
    return (0 if usable else 1, -value if direction == "desc" else value, str(row.get("symbol") or ""))


def _projection(
    conditions: Sequence[Condition], rank_by: FilterField | None, fields: Any
) -> tuple[str, ...]:
    """Which fields the answer carries: what was asked for, and what it was asked about."""

    if fields is None:
        wanted = [condition.field.name for condition in conditions]
        if rank_by is not None:
            wanted.append(rank_by.name)
        wanted.extend(["close", "change_percent", "volume_ratio"])
    elif isinstance(fields, (list, tuple)):
        wanted = [field_for(name).name for name in fields]
    else:
        raise ScreenError("fields must be a list of field names")
    seen: dict[str, None] = {}
    for name in wanted:
        seen.setdefault(name, None)
    return tuple(seen)


def screen_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    where: Any = (),
    match: str = MATCH_ALL,
    rank_by: Any = None,
    direction: str = "desc",
    limit: Any = DEFAULT_LIMIT,
    fields: Any = None,
) -> dict[str, Any]:
    """Filter, rank and project a set of rows.

    Every refusal here is a :class:`ScreenError` carrying the field or operator
    that caused it, because the caller is usually a model that can fix its own
    filter if it is told precisely what was wrong.
    """

    if match not in MATCHES:
        raise ScreenError(f"match must be {MATCH_ALL!r} or {MATCH_ANY!r}, not {match!r}")
    conditions = parse_conditions(where)
    if rank_by is None or str(rank_by).strip() == "":
        ranked: FilterField | None = None
    else:
        ranked = field_for(rank_by)
        if not ranked.numeric:
            raise ScreenError(
                f"{ranked.name} cannot be ranked on because it is not a number",
                field=ranked.name,
            )
    if direction not in ("asc", "desc"):
        raise ScreenError(f"direction must be 'asc' or 'desc', not {direction!r}")
    try:
        bounded = int(limit)
    except (TypeError, ValueError) as exc:
        raise ScreenError(f"limit must be a whole number, not {limit!r}") from exc
    if bounded < 1:
        raise ScreenError(f"limit must be at least 1, not {bounded}")
    bounded = min(bounded, MAX_LIMIT)

    candidates = list(rows)
    kept = [
        row
        for row in candidates
        if (
            all(condition.matches(row) for condition in conditions)
            if match == MATCH_ALL
            else any(condition.matches(row) for condition in conditions)
        )
    ]
    if ranked is not None:
        ordered = sorted(kept, key=lambda row: _rank_key(row, ranked, direction))
    else:
        # An empty filter is a legitimate "show me the biggest by volume", so
        # the fallback order is the stable one: by symbol.
        ordered = sorted(kept, key=lambda row: str(row.get("symbol") or ""))

    projection = _projection(conditions, ranked, fields)
    projected = [
        {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "industry": row.get("industry"),
            **{name: _value_of(row, _BY_NAME[name]) for name in projection},
        }
        for row in ordered[:bounded]
    ]
    return {
        "criteria": {
            "where": [condition.describe() for condition in conditions],
            "match": match,
            "rank_by": None if ranked is None else ranked.name,
            "direction": direction,
            "fields": list(projection),
        },
        "screened": len(candidates),
        "matched": len(kept),
        "rows": projected,
    }


def catalogue(interval: str) -> dict[str, Any]:
    """What may be filtered on, and against what."""

    groups: list[dict[str, Any]] = []
    for key, label in _GROUP_ORDER:
        members = [field.describe() for field in SCREEN_FIELDS if field.group == key]
        if members:
            groups.append({"key": key, "label": label, "fields": members})
    return {
        "interval": interval,
        "ops": {"numeric": list(NUMERIC_OPS), "equality": list(EQUALITY_OPS)},
        "match": list(MATCHES),
        "limit": {"default": DEFAULT_LIMIT, "max": MAX_LIMIT},
        "identity": list(IDENTITY_FIELDS),
        "groups": groups,
        "fields": [field.describe() for field in SCREEN_FIELDS],
        "note": (
            "Every field describes the same last closed candle of the requested "
            "timeframe: the bar's own numbers and its indicator values are read "
            "from that one bar. A value of null means there is not enough history "
            "behind it yet, and null satisfies no condition."
        ),
    }

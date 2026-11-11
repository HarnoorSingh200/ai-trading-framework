"""What an agent is.

A definition is data, not code: a system prompt, the one timeframe it decides
on, an optional set of wider timeframes it may look at for context, and the
budget and risk profile it is bounded by. It is validated once, at the edge, so
nothing downstream has to wonder whether a leverage of "4x" is a number.

Money is :class:`~decimal.Decimal` throughout and quantities are whole shares,
because the alternative is a rounding error that only shows up as a position
size nobody can explain.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from module2.config import DEFAULT_MODEL, REASONING_EFFORTS

#: The timeframes Module 1 serves. Module 1 publishes the same list at
#: ``/v1/market/timeframes`` and the worker re-checks it there at start-up; this
#: copy exists so an agent can be defined without Module 1 being up.
TIMEFRAMES: Final[tuple[str, ...]] = (
    "1m",
    "5m",
    "10m",
    "15m",
    "20m",
    "30m",
    "60m",
    "1d",
)

#: An agent id is a directory name, a URL path segment and a log prefix, so it
#: is kept boring: 3-40 characters, lower-case, and it may not begin or end with
#: a separator, so a typo at either end cannot pass for a real name.
AGENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9](?:[a-z0-9_-]{1,38}[a-z0-9])?$"
)


class DefinitionError(ValueError):
    """An agent that cannot be run as written."""


def _text(payload: dict[str, Any], key: str, default: str, *, maximum: int) -> str:
    raw = payload.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise DefinitionError(f"{key} must be text")
    value = str(raw).strip()
    if not value:
        raise DefinitionError(f"{key} must not be blank")
    if len(value) > maximum:
        raise DefinitionError(f"{key} must be at most {maximum} characters")
    return value


def _decimal(
    payload: dict[str, Any], key: str, default: str, *, low: Decimal, high: Decimal
) -> Decimal:
    raw = payload.get(key, default)
    # ``True`` is an ``int`` in Python and would sail through a naive check.
    if raw is None or isinstance(raw, bool):
        raise DefinitionError(f"{key} must be a number")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DefinitionError(f"{key} must be a number, not {raw!r}") from exc
    if not value.is_finite() or value < low or value > high:
        raise DefinitionError(f"{key} must be between {low} and {high}, not {value}")
    return value


def _whole(payload: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    raw = payload.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise DefinitionError(f"{key} must be a whole number")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise DefinitionError(f"{key} must be a whole number, not {raw!r}") from exc
    if value < low or value > high:
        raise DefinitionError(f"{key} must be between {low} and {high}, not {value}")
    return value


def _flag(payload: dict[str, Any], key: str, default: bool) -> bool:
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false", "yes", "no"}:
        return raw.strip().lower() in {"true", "yes"}
    raise DefinitionError(f"{key} must be true or false")


def _reasoning_effort(payload: dict[str, Any]) -> str | None:
    """An optional effort level for a reasoning model.

    Absent means "the process default", which is the only sensible reading of a
    definition written before reasoning was configured at all. Present but
    outside the vocabulary is an error: the provider would reject it on every
    cycle, and a typo deserves to fail here instead.
    """

    raw = payload.get("reasoning_effort")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value not in REASONING_EFFORTS:
        raise DefinitionError(
            f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}, "
            f"not {raw!r}"
        )
    return value


def _timeframe(payload: dict[str, Any], key: str, default: str) -> str:
    value = _text(payload, key, default, maximum=8)
    if value not in TIMEFRAMES:
        raise DefinitionError(
            f"{value!r} is not a timeframe Module 1 serves; it offers {', '.join(TIMEFRAMES)}"
        )
    return value


#: Every key a definition accepts. An unknown one is refused rather than
#: ignored: a typo in a field name is a setting that silently did not happen.
FIELDS: Final[tuple[str, ...]] = (
    "agent_id",
    "name",
    "system_prompt",
    "timeframe",
    "context_timeframes",
    "model",
    "reasoning_effort",
    "capital",
    "leverage",
    "risk_per_trade_pct",
    "max_capital_per_trade_pct",
    "max_hold_minutes",
    "allow_short",
    "enabled",
    "max_tool_calls_per_cycle",
    "max_tool_rounds",
    "cycle_timeout_seconds",
    "temperature",
    "max_tokens",
)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One agent, as configured by the operator."""

    agent_id: str
    name: str
    system_prompt: str
    timeframe: str = "15m"
    context_timeframes: tuple[str, ...] = ()
    model: str = DEFAULT_MODEL
    #: How hard the model is asked to reason. A null means "whatever this
    #: process was configured with", so an agent need not repeat the default.
    reasoning_effort: str | None = None
    capital: Decimal = Decimal("50000")
    leverage: Decimal = Decimal("4")
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_capital_per_trade_pct: Decimal = Decimal("20")
    max_hold_minutes: int = 60
    allow_short: bool = True
    #: Whether this agent is part of the fleet. A disabled agent stays defined -
    #: its notes, log and decisions stay readable and editable - but it is never
    #: started automatically and cannot be started by hand until it is enabled.
    enabled: bool = True
    max_tool_calls_per_cycle: int = 25
    max_tool_rounds: int = 8
    cycle_timeout_seconds: float = 120.0
    temperature: float = 0.2
    max_tokens: int = 4096

    # -- derived bounds ------------------------------------------------------ #

    @property
    def buying_power(self) -> Decimal:
        """Capital multiplied by leverage: what the agent may have in play."""

        return self.capital * self.leverage

    @property
    def risk_per_trade(self) -> Decimal:
        """Money at risk when one trade's stop is hit."""

        return self.capital * self.risk_per_trade_pct / Decimal("100")

    @property
    def max_notional(self) -> Decimal:
        """The most one position may be worth, measured against buying power."""

        return self.buying_power * self.max_capital_per_trade_pct / Decimal("100")

    @property
    def allowed_intervals(self) -> frozenset[str]:
        """The timeframes this agent may read: its own, plus what it was given."""

        return frozenset({self.timeframe, *self.context_timeframes})

    # -- serialisation ------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        """JSON-safe, and complete enough to rebuild the agent."""

        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "system_prompt": self.system_prompt,
            "timeframe": self.timeframe,
            "context_timeframes": list(self.context_timeframes),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "capital": str(self.capital),
            "leverage": str(self.leverage),
            "risk_per_trade_pct": str(self.risk_per_trade_pct),
            "max_capital_per_trade_pct": str(self.max_capital_per_trade_pct),
            "max_hold_minutes": self.max_hold_minutes,
            "allow_short": self.allow_short,
            "enabled": self.enabled,
            "max_tool_calls_per_cycle": self.max_tool_calls_per_cycle,
            "max_tool_rounds": self.max_tool_rounds,
            "cycle_timeout_seconds": self.cycle_timeout_seconds,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    def definition_hash(self) -> str:
        """A stable hash of the content, so a change is detectable afterwards."""

        payload = json.dumps(self.describe(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def replace(self, **overrides: Any) -> AgentDefinition:
        return replace(self, **overrides)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AgentDefinition:
        """Validate and build. Every refusal names the field it is about."""

        if not isinstance(payload, dict):
            raise DefinitionError("an agent definition must be an object")
        unknown = sorted(set(payload) - set(FIELDS))
        if unknown:
            raise DefinitionError(
                f"unknown setting(s): {', '.join(unknown)}; "
                f"this agent takes {', '.join(FIELDS)}"
            )

        agent_id = _text(payload, "agent_id", "", maximum=64)
        if not AGENT_ID_PATTERN.match(agent_id):
            raise DefinitionError(
                f"{agent_id!r} is not a usable agent id; use 3-40 characters of "
                "lower-case letters, digits, dashes or underscores, beginning and "
                "ending with a letter or digit"
            )

        timeframe = _timeframe(payload, "timeframe", "15m")
        raw_context = payload.get("context_timeframes") or ()
        if isinstance(raw_context, str):
            raw_context = [raw_context]
        if not isinstance(raw_context, (list, tuple)):
            raise DefinitionError("context_timeframes must be a list of timeframes")
        context: list[str] = []
        for item in raw_context:
            value = str(item).strip()
            if value not in TIMEFRAMES:
                raise DefinitionError(
                    f"{value!r} is not a timeframe Module 1 serves; "
                    f"it offers {', '.join(TIMEFRAMES)}"
                )
            if value == timeframe:
                raise DefinitionError(
                    f"{value} is this agent's own timeframe, so it does not also "
                    "need to be listed as context"
                )
            if value not in context:
                context.append(value)

        temperature = _decimal(
            payload, "temperature", "0.2", low=Decimal("0"), high=Decimal("2")
        )
        timeout = _decimal(
            payload,
            "cycle_timeout_seconds",
            "120",
            low=Decimal("1"),
            high=Decimal("900"),
        )
        return cls(
            agent_id=agent_id,
            name=_text(payload, "name", agent_id, maximum=120),
            system_prompt=_text(payload, "system_prompt", "", maximum=20_000),
            timeframe=timeframe,
            context_timeframes=tuple(context),
            model=_text(payload, "model", DEFAULT_MODEL, maximum=120),
            reasoning_effort=_reasoning_effort(payload),
            capital=_decimal(
                payload, "capital", "50000", low=Decimal("0"), high=Decimal("100000000")
            ),
            leverage=_decimal(
                payload, "leverage", "4", low=Decimal("1"), high=Decimal("100")
            ),
            risk_per_trade_pct=_decimal(
                payload,
                "risk_per_trade_pct",
                "0.5",
                low=Decimal("0"),
                high=Decimal("100"),
            ),
            max_capital_per_trade_pct=_decimal(
                payload,
                "max_capital_per_trade_pct",
                "20",
                low=Decimal("0"),
                high=Decimal("100"),
            ),
            max_hold_minutes=_whole(
                payload, "max_hold_minutes", 60, low=1, high=375
            ),
            allow_short=_flag(payload, "allow_short", True),
            enabled=_flag(payload, "enabled", True),
            max_tool_calls_per_cycle=_whole(
                payload, "max_tool_calls_per_cycle", 25, low=1, high=1000
            ),
            max_tool_rounds=_whole(payload, "max_tool_rounds", 8, low=1, high=40),
            cycle_timeout_seconds=float(timeout),
            temperature=float(temperature),
            max_tokens=_whole(payload, "max_tokens", 4096, low=128, high=32_000),
        )

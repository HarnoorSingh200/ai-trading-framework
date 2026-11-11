"""Paths, environment and tunables for Module 1.

Everything configurable is read from the environment once, at start-up, and
handed around as a frozen :class:`Settings`. Secrets are deliberately *not*
part of this object: they come from :mod:`module1.secrets`, which keeps them
out of logs and out of anything that gets serialised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from common.holidays import resolve_paths as resolve_holiday_paths

#: The module directory and the repository root that contains it.
MODULE1_DIR: Final[Path] = Path(__file__).resolve().parent
PROJECT_ROOT: Final[Path] = MODULE1_DIR.parent

DEFAULT_HOST: Final[str] = "127.0.0.1"
#: Chosen away from v2's 5100-5104 so both can run at once.
DEFAULT_PORT: Final[int] = 5201
DEFAULT_REFRESH_SECONDS: Final[float] = 60.0
DEFAULT_HISTORY_DAYS: Final[int] = 10
#: Kite's historical endpoint allows roughly three calls a second.
DEFAULT_HISTORY_RATE_LIMIT_SECONDS: Final[float] = 0.35
#: How long a websocket tick stays authoritative before history is used again.
DEFAULT_TICK_FRESH_SECONDS: Final[float] = 120.0
#: Sessions of one-minute candles fetched for every symbol at start-up. One is
#: enough to be useful immediately; deeper history is fetched on demand.
#: Six sessions is about 2,160 one-minute candles per symbol, which gives the
#: 60-minute timeframe enough bars for a momentum-30 window from the open.
#: Depth costs payload, not request count: Kite's 360-candle cap is per day and
#: one request answers several days at once.
DEFAULT_WARM_SESSIONS: Final[int] = 6
#: How often the gap-repair pass looks for minutes the feed never delivered.
DEFAULT_REPAIR_SECONDS: Final[float] = 300.0
#: The index the agents are measured against. It is a market fact like any
#: other - fetched from the broker, not hard-coded - and an index is not in the
#: tradable universe, so this one lookup is allowed to ask the dump for an
#: INDICES row.
DEFAULT_BENCHMARK_SYMBOL: Final[str] = "NIFTY 50"
DEFAULT_BENCHMARK_DAYS: Final[int] = 120
DEFAULT_BENCHMARK_REFRESH_SECONDS: Final[float] = 900.0

#: How often the holiday list is refetched from NSE. The list changes a handful
#: of times a year (a few government closures, the next year's list in
#: November-December), so once a day is generous; the fetch only runs when the
#: stored list is older than this.
DEFAULT_HOLIDAYS_REFRESH_SECONDS: Final[float] = 86400.0


def _text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    """Module 1's runtime configuration."""

    environment: str
    host: str
    port: int
    data_dir: Path
    secrets_file: Path | None
    holidays_file: Path
    holidays_seed: Path
    holidays_refresh_seconds: float
    refresh_seconds: float
    history_days: int
    history_rate_limit_seconds: float
    tick_fresh_seconds: float
    feed_enabled: bool
    warm_sessions: int
    repair_seconds: float
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL
    benchmark_days: int = DEFAULT_BENCHMARK_DAYS
    benchmark_refresh_seconds: float = DEFAULT_BENCHMARK_REFRESH_SECONDS

    def describe(self) -> dict[str, object]:
        """Safe for logs and for the UI's status panel."""

        return {
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "secrets_file": None if self.secrets_file is None else str(self.secrets_file),
            "holidays_file": str(self.holidays_file),
            "holidays_seed": str(self.holidays_seed),
            "holidays_refresh_seconds": self.holidays_refresh_seconds,
            "refresh_seconds": self.refresh_seconds,
            "history_days": self.history_days,
            "tick_fresh_seconds": self.tick_fresh_seconds,
            "feed_enabled": self.feed_enabled,
            "warm_sessions": self.warm_sessions,
            "repair_seconds": self.repair_seconds,
            "benchmark_symbol": self.benchmark_symbol,
            "benchmark_days": self.benchmark_days,
            "benchmark_refresh_seconds": self.benchmark_refresh_seconds,
        }


def load_settings() -> Settings:
    """Build the settings for this process."""

    data_dir = Path(_text("MODULE1_DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    configured = _text("FRAMEWORK_SECRETS_FILE", "")
    secrets_file = Path(configured).expanduser() if configured else None
    holidays_file, holidays_seed = resolve_holiday_paths(PROJECT_ROOT)

    return Settings(
        environment=_text("TRADING_ENV", "development"),
        host=_text("MODULE1_HOST", DEFAULT_HOST),
        port=int(_number("MODULE1_PORT", DEFAULT_PORT)),
        data_dir=data_dir,
        secrets_file=secrets_file,
        holidays_file=holidays_file,
        holidays_seed=holidays_seed,
        holidays_refresh_seconds=max(
            0.0,
            _number(
                "FRAMEWORK_HOLIDAYS_REFRESH_SECONDS", DEFAULT_HOLIDAYS_REFRESH_SECONDS
            ),
        ),
        refresh_seconds=_number("MODULE1_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS),
        history_days=int(_number("MODULE1_HISTORY_DAYS", DEFAULT_HISTORY_DAYS)),
        history_rate_limit_seconds=_number(
            "MODULE1_HISTORY_RATE_LIMIT_SECONDS", DEFAULT_HISTORY_RATE_LIMIT_SECONDS
        ),
        tick_fresh_seconds=_number("MODULE1_TICK_FRESH_SECONDS", DEFAULT_TICK_FRESH_SECONDS),
        feed_enabled=_flag("MODULE1_FEED_ENABLED", True),
        warm_sessions=int(_number("MODULE1_WARM_SESSIONS", DEFAULT_WARM_SESSIONS)),
        repair_seconds=_number("MODULE1_REPAIR_SECONDS", DEFAULT_REPAIR_SECONDS),
        benchmark_symbol=_text("MODULE1_BENCHMARK_SYMBOL", DEFAULT_BENCHMARK_SYMBOL),
        benchmark_days=int(
            _number("MODULE1_BENCHMARK_DAYS", DEFAULT_BENCHMARK_DAYS)
        ),
        benchmark_refresh_seconds=_number(
            "MODULE1_BENCHMARK_REFRESH_SECONDS", DEFAULT_BENCHMARK_REFRESH_SECONDS
        ),
    )

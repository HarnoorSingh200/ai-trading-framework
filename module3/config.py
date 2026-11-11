"""Paths, environment and tunables for Module 3.

Read once at start-up and handed around as a frozen :class:`Settings`, the same
way the other two modules do it. There is nothing secret here and nothing to
keep secret: Module 3 holds no broker credential, because it never talks to the
broker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from common import window as window_module
from common.window import TradingWindow

#: This package, and the repository root that contains it.
MODULE3_DIR: Final[Path] = Path(__file__).resolve().parent
PROJECT_ROOT: Final[Path] = MODULE3_DIR.parent

DEFAULT_HOST: Final[str] = "127.0.0.1"
#: The order book. Module 1 is 5201, Module 2 is 5202, the site is 5200.
DEFAULT_PORT: Final[int] = 5203
#: Where the price of an order's stock comes from.
DEFAULT_MODULE1_URL: Final[str] = "http://127.0.0.1:5201"
#: Where the decisions this module reconciles against come from.
DEFAULT_MODULE2_URL: Final[str] = "http://127.0.0.1:5202"

#: How often the clock is checked for a holding time running out. Time exits do
#: not wait for a tick: a quiet stock still has a deadline.
DEFAULT_SWEEP_SECONDS: Final[float] = 1.0
#: How often the watched prices are asked for outright, as a safety net under
#: the stream. A stock that never trades produces no frames at all, and an order
#: in it must not sit pending because the tape was quiet.
DEFAULT_POLL_SECONDS: Final[float] = 15.0
#: How often Module 2 is asked what it decided, so a decision made while this
#: module was down is still picked up rather than lost.
DEFAULT_RECONCILE_SECONDS: Final[float] = 30.0
#: How far back reconciliation is willing to look. Anything older belongs to a
#: session that is over, and treating it as a fresh order would book a position
#: in a market that has since closed.
DEFAULT_RECONCILE_MAX_AGE_MINUTES: Final[float] = 120.0
#: How far the market may move from the price a plan was built on before the
#: plan is abandoned. The order expires instead of entering: past this, these
#: are no longer the levels that were decided on.
DEFAULT_MAX_FILL_DRIFT_PCT: Final[float] = 1.0
#: An order is refused rather than filled on a price older than this, and
#: doubled as the freshness an exit may be judged on.
DEFAULT_STALE_PRICE_SECONDS: Final[float] = 120.0
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0


def _text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


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
    """Module 3's runtime configuration."""

    environment: str
    host: str
    port: int
    data_dir: Path
    module1_url: str
    module2_url: str
    sweep_seconds: float
    poll_seconds: float
    reconcile_seconds: float
    reconcile_max_age_minutes: float
    max_fill_drift_pct: float
    stale_price_seconds: float
    http_timeout_seconds: float
    #: When the framework goes flat. An order's holding time is capped here, so
    #: a position taken late in the day cannot outlive the operator's window.
    window: TradingWindow = field(default_factory=TradingWindow)

    def describe(self) -> dict[str, object]:
        """Safe for logs and for the UI's status panel."""

        return {
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "module1_url": self.module1_url,
            "module2_url": self.module2_url,
            "sweep_seconds": self.sweep_seconds,
            "poll_seconds": self.poll_seconds,
            "reconcile_seconds": self.reconcile_seconds,
            "reconcile_max_age_minutes": self.reconcile_max_age_minutes,
            "max_fill_drift_pct": self.max_fill_drift_pct,
            "stale_price_seconds": self.stale_price_seconds,
            "window": self.window.describe(),
        }


def load_settings() -> Settings:
    """Build the settings for this process."""

    data_dir = Path(_text("MODULE3_DATA_DIR", "data/orders"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return Settings(
        environment=_text("TRADING_ENV", "development"),
        host=_text("MODULE3_HOST", DEFAULT_HOST),
        port=int(_number("MODULE3_PORT", DEFAULT_PORT)),
        data_dir=data_dir,
        module1_url=_text("MODULE1_URL", DEFAULT_MODULE1_URL).rstrip("/"),
        module2_url=_text("MODULE2_URL", DEFAULT_MODULE2_URL).rstrip("/"),
        sweep_seconds=_number("MODULE3_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS),
        poll_seconds=_number("MODULE3_POLL_SECONDS", DEFAULT_POLL_SECONDS),
        reconcile_seconds=_number("MODULE3_RECONCILE_SECONDS", DEFAULT_RECONCILE_SECONDS),
        reconcile_max_age_minutes=_number(
            "MODULE3_RECONCILE_MAX_AGE_MINUTES", DEFAULT_RECONCILE_MAX_AGE_MINUTES
        ),
        max_fill_drift_pct=_number(
            "MODULE3_MAX_FILL_DRIFT_PCT", DEFAULT_MAX_FILL_DRIFT_PCT
        ),
        stale_price_seconds=_number(
            "MODULE3_STALE_PRICE_SECONDS", DEFAULT_STALE_PRICE_SECONDS
        ),
        http_timeout_seconds=_number(
            "MODULE3_HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT_SECONDS
        ),
        window=window_module.load(),
    )

"""Paths, environment and tunables for Module 2.

Read once at start-up and handed around as a frozen :class:`Settings`, the same
way Module 1 does it. Nothing secret lives here: the model credential comes from
the environment or from ``FRAMEWORK_SECRETS_FILE``, which lives outside the
repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from common import window as window_module
from common.window import TradingWindow

#: This package, and the repository root that contains it.
MODULE2_DIR: Final[Path] = Path(__file__).resolve().parent
PROJECT_ROOT: Final[Path] = MODULE2_DIR.parent

DEFAULT_HOST: Final[str] = "127.0.0.1"
#: The control surface. Module 1 is 5201 and the site is 5200.
DEFAULT_PORT: Final[int] = 5202
DEFAULT_MODULE1_URL: Final[str] = "http://127.0.0.1:5201"
#: Where a decision is handed on to, so it becomes a tracked position. The
#: hand-over is best-effort: Module 3 reconciles against Module 2's own log, so
#: a decision taken while it was down is still picked up rather than lost.
DEFAULT_MODULE3_URL: Final[str] = "http://127.0.0.1:5203"
DEFAULT_LLM_BASE_URL: Final[str] = "https://api.deepseek.com"
#: The provider accepts this string and answers as ``deepseek-flash``.
DEFAULT_MODEL: Final[str] = "deepseek-v4-flash"
DEFAULT_OPENAI_BASE_URL: Final[str] = "https://api.openai.com/v1"

#: How the model is spoken to.
#:
#: "auto" picks the Responses API for GPT models that cannot do reasoning and
#: tool calling together on Chat Completions (GPT-5.4 and later), and Chat
#: Completions everywhere else. "chat" and "responses" force one, which is what
#: a gateway that implements only one of them needs.
LLM_APIS: Final[tuple[str, ...]] = ("auto", "chat", "responses")
DEFAULT_LLM_API: Final[str] = "auto"

#: How hard a reasoning model is asked to think. "none" still allows function
#: calling (on Chat Completions it is the *only* effort that does), but the
#: choice is the model's - the framework records only what it asked for.
REASONING_EFFORTS: Final[tuple[str, ...]] = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
DEFAULT_REASONING_EFFORT: Final[str] = "high"

#: How long to wait for Module 1 to finish building the bar that just closed.
#: This was a flat 45-second sleep with no evidence behind it; it is now the
#: ceiling on a wait that ends the moment the book says the bar is done, which
#: is usually about a second.
DEFAULT_BAR_TIMEOUT_SECONDS: Final[float] = 12.0
#: How much of the universe must have a finished bar before the agent starts.
#: Not all of it: a stock that has not traded for a minute is legitimately
#: short, and waiting for fifty out of fifty would mean waiting for ever.
DEFAULT_BAR_READY_FRACTION: Final[float] = 0.9
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
#: How long the worker waits for Module 1 to be ready before saying so.
DEFAULT_READY_TIMEOUT_SECONDS: Final[float] = 180.0
#: How long after a bar closes the agent waits before reading it. Module 1
#: closes a minute when the first tick of the next one arrives, so the bar that
#: just ended is normally complete within about a second - this is the buffer
#: that makes "about a second" not a guess, and it is the same five seconds the
#: operator asked for.
DEFAULT_CANDLE_SETTLE_SECONDS: Final[float] = 5.0
#: Whether the control service starts every defined agent when it comes up.
#: The operator asked for the fleet to run itself; a false here is for testing.
DEFAULT_AUTOSTART: Final[bool] = True


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


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """One of a fixed vocabulary, or the default when it is not.

    A typo in an operator's env file is named once, at start-up, rather than
    turning into a request the provider refuses on every cycle.
    """

    value = (os.environ.get(name) or "").strip().lower()
    return value if value in allowed else default


@dataclass(frozen=True, slots=True)
class Settings:
    """Module 2's runtime configuration."""

    environment: str
    host: str
    port: int
    data_dir: Path
    secrets_file: Path | None
    module1_url: str
    module3_url: str
    llm_base_url: str
    model: str
    bar_timeout_seconds: float
    bar_ready_fraction: float
    http_timeout_seconds: float
    ready_timeout_seconds: float
    #: When the framework may decide, and when everything must be flat.
    window: TradingWindow = field(default_factory=TradingWindow)
    #: Seconds after a bar close before the bar is read. Module 1 needs the
    #: first tick of the next minute to close the last one, so the candle and
    #: its indicators are given a moment to exist before an agent looks.
    candle_settle_seconds: float = DEFAULT_CANDLE_SETTLE_SECONDS
    #: Start every defined agent when the control service starts.
    autostart: bool = DEFAULT_AUTOSTART
    #: Used by agents whose model name begins with ``gpt-`` when an OpenAI key
    #: is present. A gateway can still serve those names through the default
    #: endpoint when only the default credential is configured.
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    #: "auto", "chat" or "responses": which model API a request is sent to.
    llm_api: str = DEFAULT_LLM_API
    #: The reasoning effort asked of a reasoning model, unless the agent's own
    #: definition overrides it. Recorded with every decision, so the thinking a
    #: decision cost can be read back later.
    reasoning_effort: str = DEFAULT_REASONING_EFFORT

    def describe(self) -> dict[str, object]:
        """Safe for logs and for the UI's status panel."""

        return {
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "secrets_file": None if self.secrets_file is None else str(self.secrets_file),
            "module1_url": self.module1_url,
            "module3_url": self.module3_url,
            "llm_base_url": self.llm_base_url,
            "openai_base_url": self.openai_base_url,
            "model": self.model,
            "llm_api": self.llm_api,
            "reasoning_effort": self.reasoning_effort,
            "bar_timeout_seconds": self.bar_timeout_seconds,
            "bar_ready_fraction": self.bar_ready_fraction,
            "window": self.window.describe(),
            "candle_settle_seconds": self.candle_settle_seconds,
            "autostart": self.autostart,
        }


def load_settings() -> Settings:
    """Build the settings for this process."""

    data_dir = Path(_text("MODULE2_DATA_DIR", "data/agents"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    configured = _text("FRAMEWORK_SECRETS_FILE", "")
    secrets_file = Path(configured).expanduser() if configured else None

    return Settings(
        environment=_text("TRADING_ENV", "development"),
        host=_text("MODULE2_HOST", DEFAULT_HOST),
        port=int(_number("MODULE2_PORT", DEFAULT_PORT)),
        data_dir=data_dir,
        secrets_file=secrets_file,
        module1_url=_text("MODULE1_URL", DEFAULT_MODULE1_URL).rstrip("/"),
        module3_url=_text("MODULE3_URL", DEFAULT_MODULE3_URL).rstrip("/"),
        llm_base_url=_text("MODULE2_LLM_BASE_URL", DEFAULT_LLM_BASE_URL).rstrip("/"),
        model=_text("MODULE2_MODEL", DEFAULT_MODEL),
        # The old name said "sleep this long and hope". It is read as a fallback
        # so an existing .env keeps working, but it now means the ceiling on a
        # wait that ends as soon as the bar is actually built.
        bar_timeout_seconds=_number(
            "MODULE2_BAR_TIMEOUT_SECONDS",
            _number("MODULE2_SETTLE_SECONDS", DEFAULT_BAR_TIMEOUT_SECONDS),
        ),
        bar_ready_fraction=_number(
            "MODULE2_BAR_READY_FRACTION", DEFAULT_BAR_READY_FRACTION
        ),
        http_timeout_seconds=_number(
            "MODULE2_HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT_SECONDS
        ),
        ready_timeout_seconds=_number(
            "MODULE2_READY_TIMEOUT_SECONDS", DEFAULT_READY_TIMEOUT_SECONDS
        ),
        window=window_module.load(),
        candle_settle_seconds=_number(
            "MODULE2_CANDLE_SETTLE_SECONDS", DEFAULT_CANDLE_SETTLE_SECONDS
        ),
        autostart=_flag("MODULE2_AUTOSTART", DEFAULT_AUTOSTART),
        openai_base_url=_text(
            "MODULE2_OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL
        ).rstrip("/"),
        llm_api=_choice("MODULE2_LLM_API", DEFAULT_LLM_API, LLM_APIS),
        reasoning_effort=_choice(
            "MODULE2_REASONING_EFFORT", DEFAULT_REASONING_EFFORT, REASONING_EFFORTS
        ),
    )

"""What the runner needs to know: paths, addresses, and the day's clock."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from common import env
from common.holidays import resolve_paths as resolve_holiday_paths
from common.window import TradingWindow
from common.window import load as load_window

#: The repository root: this file lives in ``<root>/runner/config.py``.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

DEFAULT_MODULE1_URL: Final[str] = "http://127.0.0.1:5201"
DEFAULT_MODULE2_URL: Final[str] = "http://127.0.0.1:5202"
DEFAULT_MODULE3_URL: Final[str] = "http://127.0.0.1:5203"
DEFAULT_MODULE4_URL: Final[str] = "http://127.0.0.1:5204"

#: The runner's own control surface: the Control tab of the operator site
#: reads the process overview, the logs and the restart buttons from here.
#: It is the runner's address, not a fifth module's - no market data, no
#: positions, no decisions live behind it.
DEFAULT_CONTROL_HOST: Final[str] = "127.0.0.1"
DEFAULT_CONTROL_PORT: Final[int] = 5205

#: How long Module 1 is given to come up before the rest is started, and the
#: ceiling on that wait. The floor is the operator's "one minute to stabilise";
#: the ceiling is what stops a broker outage from hanging the morning.
DEFAULT_STABILIZE_SECONDS: Final[float] = 60.0
DEFAULT_STABILIZE_TIMEOUT_SECONDS: Final[float] = 420.0
#: The share of the universe that must have computed indicators before the
#: rest of the framework may start. Not all of it: a symbol whose history did
#: not come back is reported in Module 1's own status and must not hold the
#: morning hostage, but "some indicators" is a different claim from "the
#: universe is covered", and this is the line between them.
DEFAULT_INDICATOR_READY_FRACTION: Final[float] = 0.9
#: How long before the decision window the day's services (Modules 3 and 2)
#: are started, so the price subscriptions and the agent clocks are already
#: in place when the first candle of the day is decided.
DEFAULT_DAY_LEAD_SECONDS: Final[float] = 300.0
#: How long after the decision window an agent's last cycle may still finish.
DEFAULT_MODULE2_STOP_GRACE_SECONDS: Final[float] = 60.0
#: How long after the framework close the services are given to settle.
DEFAULT_CLOSE_GRACE_SECONDS: Final[float] = 20.0
#: How often each service's /health is probed, and how many restarts a single
#: service gets before the runner stops trying and says so.
DEFAULT_HEALTH_SECONDS: Final[float] = 30.0
DEFAULT_RESTART_LIMIT: Final[int] = 5
DEFAULT_RESTART_DELAY_SECONDS: Final[float] = 5.0
#: What "power the machine off" means, for a machine that is scheduled to
#: shut itself down. A command line, never a shell string; on an OCI instance
#: it needs the NOPASSWD sudoers line deploy/README.md describes.
DEFAULT_SHUTDOWN_COMMAND: Final[str] = "sudo shutdown -h now"


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    """Everything the runner reads from the environment, read once."""

    root: Path
    python: str
    log_dir: Path
    module1_url: str
    module2_url: str
    module3_url: str
    module4_url: str
    control_host: str
    control_port: int
    window: TradingWindow
    #: The local exchange calendar: the live holiday file Module 1 refreshes,
    #: and the checked-in seed it falls back to. The runner reads this list
    #: directly; it does not ask Module 1 whether the market is open.
    holidays_file: Path
    holidays_seed: Path
    stabilize_seconds: float
    stabilize_timeout_seconds: float
    indicator_ready_fraction: float
    day_lead_seconds: float
    module2_stop_grace_seconds: float
    close_grace_seconds: float
    health_seconds: float
    restart_limit: int
    restart_delay_seconds: float
    #: When true, after the day's close the runner stops every remaining
    #: service, checks that each process is really gone, and only then runs
    #: ``shutdown_command``. This is what makes a scheduled machine pay for
    #: nothing outside its hours; it is off by default because a development
    #: machine must never power itself off by accident.
    self_shutdown: bool = False
    #: The command that powers the machine off: split like a command line and
    #: never run through a shell, so this setting is a command, not a script.
    shutdown_command: str = DEFAULT_SHUTDOWN_COMMAND

    def urls(self) -> dict[str, str]:
        return {
            "module1": self.module1_url,
            "module2": self.module2_url,
            "module3": self.module3_url,
            "module4": self.module4_url,
        }

    @property
    def control_url(self) -> str:
        return f"http://{self.control_host}:{self.control_port}"

    def describe(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "log_dir": str(self.log_dir),
            "modules": self.urls(),
            "control": self.control_url,
            "window": self.window.describe(),
            "holidays_file": str(self.holidays_file),
            "holidays_seed": str(self.holidays_seed),
            "stabilize_seconds": self.stabilize_seconds,
            "stabilize_timeout_seconds": self.stabilize_timeout_seconds,
            "indicator_ready_fraction": self.indicator_ready_fraction,
            "day_lead_seconds": self.day_lead_seconds,
            "restart_limit": self.restart_limit,
            "self_shutdown": self.self_shutdown,
            "shutdown_command": self.shutdown_command,
        }


def load_settings(*, root: Path | None = None) -> RunnerSettings:
    """Build the runner's settings from the environment."""

    resolved_root = Path(root or PROJECT_ROOT)
    log_dir = Path(env.text("FRAMEWORK_LOG_DIR", "logs")).expanduser()
    if not log_dir.is_absolute():
        log_dir = resolved_root / log_dir
    holidays_file, holidays_seed = resolve_holiday_paths(resolved_root)
    return RunnerSettings(
        root=resolved_root,
        python=env.text("FRAMEWORK_PYTHON", sys.executable),
        log_dir=log_dir,
        module1_url=env.text("MODULE1_URL", DEFAULT_MODULE1_URL).rstrip("/"),
        module2_url=env.text("MODULE2_URL", DEFAULT_MODULE2_URL).rstrip("/"),
        module3_url=env.text("MODULE3_URL", DEFAULT_MODULE3_URL).rstrip("/"),
        module4_url=env.text("MODULE4_URL", DEFAULT_MODULE4_URL).rstrip("/"),
        control_host=env.text("FRAMEWORK_CONTROL_HOST", DEFAULT_CONTROL_HOST),
        control_port=env.integer("FRAMEWORK_CONTROL_PORT", DEFAULT_CONTROL_PORT),
        window=load_window(),
        holidays_file=holidays_file,
        holidays_seed=holidays_seed,
        stabilize_seconds=max(
            0.0, env.number("FRAMEWORK_STABILIZE_SECONDS", DEFAULT_STABILIZE_SECONDS)
        ),
        stabilize_timeout_seconds=max(
            5.0,
            env.number(
                "FRAMEWORK_STABILIZE_TIMEOUT_SECONDS", DEFAULT_STABILIZE_TIMEOUT_SECONDS
            ),
        ),
        indicator_ready_fraction=min(
            1.0,
            max(
                0.0,
                env.number(
                    "FRAMEWORK_INDICATOR_READY_FRACTION",
                    DEFAULT_INDICATOR_READY_FRACTION,
                ),
            ),
        ),
        day_lead_seconds=max(
            0.0, env.number("FRAMEWORK_DAY_LEAD_SECONDS", DEFAULT_DAY_LEAD_SECONDS)
        ),
        module2_stop_grace_seconds=max(
            0.0,
            env.number(
                "FRAMEWORK_MODULE2_STOP_GRACE_SECONDS",
                DEFAULT_MODULE2_STOP_GRACE_SECONDS,
            ),
        ),
        close_grace_seconds=max(
            0.0, env.number("FRAMEWORK_CLOSE_GRACE_SECONDS", DEFAULT_CLOSE_GRACE_SECONDS)
        ),
        health_seconds=max(
            5.0, env.number("FRAMEWORK_HEALTH_SECONDS", DEFAULT_HEALTH_SECONDS)
        ),
        restart_limit=max(0, env.integer("FRAMEWORK_RESTART_LIMIT", DEFAULT_RESTART_LIMIT)),
        restart_delay_seconds=max(
            0.0,
            env.number("FRAMEWORK_RESTART_DELAY_SECONDS", DEFAULT_RESTART_DELAY_SECONDS),
        ),
        self_shutdown=env.flag("FRAMEWORK_SELF_SHUTDOWN", False),
        shutdown_command=env.text(
            "FRAMEWORK_SHUTDOWN_COMMAND", DEFAULT_SHUTDOWN_COMMAND
        ),
    )

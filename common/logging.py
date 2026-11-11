"""One logging setup for every process in the framework.

The framework runs unattended on a machine nobody is looking at, so the log
file is the primary record and the console is the convenience. Every service
gets its own rotating file under ``logs/``, stamped in IST (the exchange's
clock, which is the one every other number in this project is measured
against), and a failure to open the file must not stop a service from starting.

One exception: uvicorn's own loggers are left to propagate here, so a request
or a startup message lands in the same file as the module's own lines instead
of in a second, differently-shaped stream.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from common import env

IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))

DEFAULT_LOG_DIR: Final[str] = "logs"
DEFAULT_MAX_BYTES: Final[int] = 5_000_000
DEFAULT_BACKUPS: Final[int] = 5
DEFAULT_FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
DEFAULT_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


class _IstFormatter(logging.Formatter):
    """A formatter whose timestamps are IST whatever the machine's clock is."""

    def converter(self, timestamp: float) -> object:  # noqa: D102 - logging hook
        return datetime.fromtimestamp(timestamp, IST).timetuple()


def log_directory() -> Path:
    """Where log files live: ``FRAMEWORK_LOG_DIR``, else ``logs/``."""

    configured = env.text("FRAMEWORK_LOG_DIR", DEFAULT_LOG_DIR)
    return Path(configured).expanduser()


def setup_logging(
    service: str,
    *,
    directory: str | Path | None = None,
    level: int | str | None = None,
    console: bool = True,
    filename: str | None = None,
) -> logging.Logger:
    """Configure the root logger and return the logger for ``service``.

    Called once at the top of every entry point. Repeating it is safe: the
    handlers are replaced rather than stacked, so a service that is started
    twice in one process (a test, or an agent's child process) does not write
    every line twice.
    """

    resolved_level = _level(level)
    formatter = _IstFormatter(DEFAULT_FORMAT, DEFAULT_DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(resolved_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with suppress(Exception):
            handler.close()

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    target = Path(directory) if directory is not None else log_directory()
    name = filename or f"{service}.log"
    try:
        target.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            target / name,
            maxBytes=DEFAULT_MAX_BYTES,
            backupCount=DEFAULT_BACKUPS,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
    except OSError as exc:  # pragma: no cover - a read-only disk is reported elsewhere
        logging.getLogger(service).warning(
            "the log file under %s could not be opened (%s); logging to the console only",
            target,
            type(exc).__name__,
        )

    # Third-party libraries are chatty at INFO and say nothing a trading log
    # needs; their warnings and errors still come through.
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


def _level(value: int | str | None) -> int:
    if isinstance(value, int):
        return value
    text = str(value or os.environ.get("FRAMEWORK_LOG_LEVEL") or "INFO").strip().upper()
    resolved = logging.getLevelName(text)
    return resolved if isinstance(resolved, int) else logging.INFO

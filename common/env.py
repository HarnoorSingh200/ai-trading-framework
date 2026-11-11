"""Environment and secret reading, shared by every service.

The rule is the one Module 1 and Module 2 already follow, kept in one place so
the runner and the notifier can follow it too: a secret lives in the process
environment, or in the ``KEY=VALUE`` file named by ``FRAMEWORK_SECRETS_FILE``,
which lives outside the repository. Nothing here ever logs a value, writes one
back, or returns one to a screen.

Ordinary settings are read the same way, and read once per call: a process that
is already running does not get to see an edited environment, and pretending
otherwise would make a restart and a change look the same.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Final

#: The name of the file that holds secrets, when the environment does not.
FRAMEWORK_SECRETS_FILE: Final[str] = "FRAMEWORK_SECRETS_FILE"

_lock = threading.Lock()
_file_values: dict[str, str] | None = None
_file_path: Path | None = None


def text(name: str, default: str = "") -> str:
    """An environment value, stripped, or ``default`` when blank."""

    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def flag(name: str, default: bool = False) -> bool:
    """A boolean environment value; anything unreadable keeps the default."""

    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def number(name: str, default: float) -> float:
    """A float environment value; anything unreadable keeps the default."""

    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def integer(name: str, default: int) -> int:
    """A whole-number environment value; anything unreadable keeps the default."""

    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def secrets_path() -> Path | None:
    """The secrets file named by the environment, if one is named."""

    configured = text(FRAMEWORK_SECRETS_FILE)
    return Path(configured).expanduser() if configured else None


def _load_file() -> dict[str, str]:
    """Read the secrets file once, tolerating comments, blanks and quotes."""

    global _file_values, _file_path
    path = secrets_path()
    with _lock:
        if _file_values is not None and _file_path == path:
            return _file_values
        values: dict[str, str] = {}
        if path is not None and path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = ""
            for line in content.splitlines():
                body = line.strip()
                if not body or body.startswith("#"):
                    continue
                if body.startswith("export "):
                    body = body[len("export ") :].strip()
                name, separator, value = body.partition("=")
                if not separator:
                    continue
                cleaned = value.strip().strip("'\"")
                if cleaned:
                    values[name.strip()] = cleaned
        _file_values = values
        _file_path = path
        return values


def reload_secrets() -> None:
    """Forget the file cache so the next read sees an edited file."""

    global _file_values
    with _lock:
        _file_values = None


def secret(name: str) -> str | None:
    """A secret's value: the environment first, then the secrets file."""

    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    return _load_file().get(name)


def require(name: str) -> str:
    """The secret, or an error that names it without ever printing a value."""

    value = secret(name)
    if value is None:
        where = secrets_path() or "the environment"
        raise RuntimeError(f"{name} is not set in the environment or in {where}")
    return value


def describe(names: tuple[str, ...] = ()) -> dict[str, Any]:
    """Which names are present. Never a value, never even a length."""

    path = secrets_path()
    return {
        "file": None if path is None else str(path),
        "file_exists": None if path is None else path.exists(),
        "present": {name: secret(name) is not None for name in names},
    }

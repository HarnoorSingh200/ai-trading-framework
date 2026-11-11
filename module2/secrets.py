"""Secret loading for Module 2.

The same shape as Module 1's, deliberately, and for the same reason: a secret
lives outside the repository, is read at the moment it is needed, and is never
logged, never serialised and never handed back over HTTP. What leaves this
module is either the value itself or a yes/no about its presence.

Module 2's only secret is the model's API key. It holds no broker credential of
any kind.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

#: The model provider's key.
LLM_API_KEY: Final[str] = "DEEPSEEK_API_KEY"

#: Used only by agents whose model uses the OpenAI route.
OPENAI_API_KEY: Final[str] = "OPENAI_API_KEY"

ALL_NAMES: Final[tuple[str, ...]] = (LLM_API_KEY, OPENAI_API_KEY)


class MissingSecret(RuntimeError):
    """A secret the caller cannot proceed without is not configured."""

    def __init__(self, name: str, *, where: object = None) -> None:
        location = "the environment" if where is None else str(where)
        super().__init__(f"{name} is not set in {location}")
        self.name = name
        self.where = where


class SecretStore:
    """Environment first, then a ``KEY=VALUE`` file outside the repository."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path is not None else None
        self._file_values: dict[str, str] | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    def describe(self) -> dict[str, object]:
        """Which names are present. Never a value, never even a length."""

        return {
            "file": None if self._path is None else str(self._path),
            "file_exists": None if self._path is None else self._path.exists(),
            "present": {name: self.get(name) is not None for name in ALL_NAMES},
        }

    def _load_file(self) -> dict[str, str]:
        if self._file_values is not None:
            return self._file_values
        values: dict[str, str] = {}
        path = self._path
        if path is not None and path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            for line in text.splitlines():
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
        self._file_values = values
        return values

    def reload(self) -> None:
        """Forget the file cache so the next read sees an edited file."""

        self._file_values = None

    def get(self, name: str) -> str | None:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
        return self._load_file().get(name)

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise MissingSecret(name, where=self._path or "the environment")
        return value

"""The broker session cache, kept outside the repository.

Kite's web session is not tied to the trading day the way a Kite Connect
access token is: it behaves like a browser tab that stays signed in for weeks
until something signs it out. Caching it on disk is therefore what makes a
restart unattended - the module reloads the session it already has instead of
signing in again.

The file format deliberately matches v2's cache, at the same path. Two
processes that sign in independently invalidate each other's sessions at Kite,
so sharing one cache file is safer than keeping two copies of the truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

from module1.timeutil import format_utc, parse_timestamp, to_ist, to_utc, utc_now

#: Environment override for the cache location.
CACHE_PATH_ENV: Final[str] = "FRAMEWORK_TOKEN_CACHE"

#: ``enctoken`` is the website session; ``access_token`` is the documented
#: Kite Connect token that dies at dawn. The two authenticate different hosts.
SESSION_KIND_WEB: Final[str] = "enctoken"
SESSION_KIND_ACCESS: Final[str] = "access_token"

#: A web session is assumed to last this long. The first call Kite refuses
#: ends it earlier, which is what the session manager reacts to.
WEB_SESSION_DAYS: Final[int] = 30


def default_cache_path() -> Path:
    """The platform default cache location, outside the repository."""

    override = os.environ.get(CACHE_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
        return base / "trading-framework" / "token_cache.json"
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "trading-framework" / "token_cache.json"


def fingerprint(value: str) -> str:
    """A short, non-reversible fingerprint, used to spot a changed API key."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class TokenEntry:
    """One cached broker session."""

    broker: str
    api_key_fingerprint: str
    user_id: str
    access_token: str
    issued_at: datetime
    expires_at: datetime
    session_date: date
    kind: str = SESSION_KIND_WEB

    def is_valid(self, *, now: datetime | None = None) -> bool:
        return self.expires_at > to_utc(now or utc_now())

    def public_view(self) -> dict[str, object]:
        """Safe to log, safe to serve: no token, only its fingerprint."""

        return {
            "broker": self.broker,
            "user_id": self.user_id,
            "kind": self.kind,
            "token_fingerprint": fingerprint(self.access_token),
            "issued_at": format_utc(self.issued_at),
            "expires_at": format_utc(self.expires_at),
            "session_date": self.session_date.isoformat(),
            "valid": self.is_valid(),
        }


class TokenCache:
    """Reads and writes the session cache file."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        project_root: Path | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else default_cache_path()
        self._root = Path(project_root).resolve() if project_root is not None else None
        self._assert_outside_repository()

    @property
    def path(self) -> Path:
        return self._path

    def _assert_outside_repository(self) -> None:
        """A session token must never be one ``git add`` away from leaking."""

        if self._root is None:
            return
        try:
            self._path.resolve().relative_to(self._root)
        except ValueError:
            return
        raise ValueError(f"the token cache must live outside the repository: {self._path}")

    def load(self) -> TokenEntry | None:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return TokenEntry(
                broker=str(raw["broker"]),
                api_key_fingerprint=str(raw["api_key_fingerprint"]),
                user_id=str(raw["user_id"]),
                access_token=str(raw["access_token"]),
                issued_at=parse_timestamp(str(raw["issued_at"])),
                expires_at=parse_timestamp(str(raw["expires_at"])),
                session_date=date.fromisoformat(str(raw["session_date"])),
                kind=str(raw.get("kind") or SESSION_KIND_WEB),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def store(self, entry: TokenEntry) -> None:
        """Write the entry atomically, with restrictive permissions."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "broker": entry.broker,
            "api_key_fingerprint": entry.api_key_fingerprint,
            "user_id": entry.user_id,
            "access_token": entry.access_token,
            "issued_at": format_utc(entry.issued_at),
            "expires_at": format_utc(entry.expires_at),
            "session_date": entry.session_date.isoformat(),
            "kind": entry.kind,
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".token_cache-", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path = Path(temporary)
            try:
                temporary_path.chmod(0o600)
            except OSError:  # pragma: no cover - Windows honours this loosely
                pass
            temporary_path.replace(self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)

    def valid_entry(
        self,
        *,
        api_key_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> TokenEntry | None:
        """The cached entry, if it is unexpired and belongs to this API key."""

        entry = self.load()
        if entry is None:
            return None
        if api_key_fingerprint and entry.api_key_fingerprint != api_key_fingerprint:
            return None
        return entry if entry.is_valid(now=now) else None

    def describe(self) -> dict[str, object]:
        entry = self.load()
        return {
            "path": str(self._path),
            "present": entry is not None,
            "entry": None if entry is None else entry.public_view(),
        }


def web_session_expiry(moment: datetime | None = None) -> datetime:
    """When a web session minted at ``moment`` is assumed to expire."""

    return to_utc(moment or utc_now()) + timedelta(days=WEB_SESSION_DAYS)


def session_day(moment: datetime) -> date:
    """The IST calendar day a session was opened on."""

    return to_ist(moment).date()

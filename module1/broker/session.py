"""Unattended Zerodha sign-in.

This module exists to meet one requirement: the framework must sign in to the
broker on its own, on every start, without a person approving anything.

Kite offers two ways in, and they are not interchangeable:

* The documented Kite Connect flow needs the operator to open a consent page
  in a browser, and the token it mints dies at 06:00 IST the next morning.
* The website's own flow is a user id, a password and a TOTP code. What comes
  back is the ``enctoken`` cookie a browser tab would be holding. It is fully
  scriptable, and it survives for weeks rather than for a night.

This module uses the second one. It caches the session outside the repository
and re-uses it until the broker refuses it, at which point it signs in again by
itself. The resolution order on every start is: this process's memory, then the
shared cache on disk, then a fresh login.

Two details matter for correctness:

* sign-in is single-flight. Kite invalidates a previous session when a new one
  is minted for the same user, so two concurrent logins would leave this
  process holding a token that the second one just killed.
* a refusal from the broker is healed from the shared cache *first*. If
  another process has already signed in, its session is newer and better than
  anything this process could mint.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import httpx
import pyotp

from module1.broker.token_cache import (
    SESSION_KIND_ACCESS,
    SESSION_KIND_WEB,
    TokenCache,
    TokenEntry,
    fingerprint,
    session_day,
    web_session_expiry,
)
from module1.secrets import (
    ZERODHA_ACCESS_TOKEN,
    ZERODHA_API_KEY,
    ZERODHA_PASSWORD,
    ZERODHA_TOTP_SECRET,
    ZERODHA_USER_ID,
    MissingSecret,
    SecretStore,
)
from module1.timeutil import format_utc, to_utc, utc_now

KITE_WEB_BASE_URL: Final[str] = "https://kite.zerodha.com"
KITE_LOGIN_URL: Final[str] = f"{KITE_WEB_BASE_URL}/api/login"
KITE_TWOFA_URL: Final[str] = f"{KITE_WEB_BASE_URL}/api/twofa"
KITE_WEB_PROFILE_URL: Final[str] = f"{KITE_WEB_BASE_URL}/oms/user/profile"

KITE_API_BASE_URL: Final[str] = "https://api.kite.trade"
KITE_PROFILE_URL: Final[str] = f"{KITE_API_BASE_URL}/user/profile"

KITE_VERSION_HEADER: Final[tuple[str, str]] = ("X-Kite-Version", "3")
DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0


class BrokerAuthError(RuntimeError):
    """Sign-in could not be completed. The message never contains a secret."""


@dataclass(slots=True)
class SessionStatus:
    """What the module knows about its own sign-in state."""

    user_id: str | None = None
    kind: str | None = None
    source: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    valid: bool = False
    login_count: int = 0
    last_login_at: datetime | None = None
    last_error: str | None = None
    cache_path: str | None = None
    last_check_at: datetime | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "kind": self.kind,
            "source": self.source,
            "valid": self.valid,
            "issued_at": None if self.issued_at is None else format_utc(self.issued_at),
            "expires_at": None if self.expires_at is None else format_utc(self.expires_at),
            "login_count": self.login_count,
            "last_login_at": (
                None if self.last_login_at is None else format_utc(self.last_login_at)
            ),
            "last_check_at": (
                None if self.last_check_at is None else format_utc(self.last_check_at)
            ),
            "last_error": self.last_error,
            "cache_path": self.cache_path,
            "unattended": True,
        }


@dataclass(slots=True)
class _Held:
    """The session this process is currently holding."""

    token: str
    kind: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    source: str


@dataclass(slots=True)
class SessionManager:
    """Holds, validates and repairs the broker session."""

    secrets: SecretStore
    cache: TokenCache | None = None
    client: httpx.Client | None = None
    clock: Callable[[], datetime] = utc_now
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    #: Re-entrant: ``ensure`` holds it while it decides, and the sign-in it
    #: delegates to takes it again to stay single-flight.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _held: _Held | None = field(default=None, repr=False)
    _status: SessionStatus = field(default_factory=SessionStatus, repr=False)

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = TokenCache()
        if self.client is None:
            self.client = httpx.Client(
                timeout=self.timeout_seconds, follow_redirects=True
            )
        self._status.cache_path = str(self.cache.path)

    # -- public surface ----------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        """Safe for logs and for the UI."""

        return self._status.describe()

    def token(self) -> str:
        """A usable session token, signing in only if it has to."""

        return self.ensure().token

    def ensure(self, *, force: bool = False) -> _Held:
        """Return a session the broker will accept."""

        with self._lock:
            if not force:
                held = self._held
                if held is not None and held.expires_at > to_utc(self.clock()):
                    self._status.valid = True
                    return held
                from_cache = self._from_cache()
                if from_cache is not None:
                    return self._adopt(from_cache, source="cache", validate=True)
                from_secret = self._from_configured_token()
                if from_secret is not None:
                    return self._adopt(from_secret, source="configured_token", validate=True)
            return self._adopt(self._login(), source="login", validate=False)

    def heal(self) -> _Held:
        """Recover after the broker refused the session we were holding.

        The shared cache is consulted before signing in: another process may
        have signed in already, and its session is newer than ours. Signing in
        here would kill that one instead of adopting it.
        """

        with self._lock:
            self._held = None
            self._status.valid = False
            from_cache = self._from_cache()
            if from_cache is not None:
                return self._adopt(from_cache, source="cache_recovery", validate=True)
            return self._adopt(self._login(), source="login_recovery", validate=False)

    def logout(self) -> None:
        """Forget the cached session. The broker session itself still stands."""

        with self._lock:
            self._held = None
            self._status.valid = False
            if self.cache is not None:
                self.cache.clear()

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    # -- adoption ----------------------------------------------------------- #

    def _adopt(self, entry: TokenEntry, *, source: str, validate: bool) -> _Held:
        if validate and not self._validates(entry):
            self._status.last_error = "the broker refused the stored session"
            if source == "login":
                raise BrokerAuthError("the broker refused the session it just issued")
            # A stored session that no longer works is worth replacing once.
            return self._adopt(self._login(), source="login", validate=False)

        held = _Held(
            token=entry.access_token,
            kind=entry.kind,
            user_id=entry.user_id,
            issued_at=entry.issued_at,
            expires_at=entry.expires_at,
            source=source,
        )
        self._held = held
        self._status.user_id = held.user_id
        self._status.kind = held.kind
        self._status.source = source
        self._status.issued_at = held.issued_at
        self._status.expires_at = held.expires_at
        self._status.valid = True
        self._status.last_check_at = to_utc(self.clock())
        self._status.last_error = None
        return held

    def _from_cache(self) -> TokenEntry | None:
        """The cached session, when it is unexpired and for this API key."""

        if self.cache is None:
            return None
        return self.cache.valid_entry(api_key_fingerprint=self._api_key_fingerprint())

    def _from_configured_token(self) -> TokenEntry | None:
        """An operator-pasted Kite Connect token, when one is configured."""

        configured = self.secrets.get(ZERODHA_ACCESS_TOKEN)
        if not configured:
            return None
        now = to_utc(self.clock())
        return TokenEntry(
            broker="zerodha",
            api_key_fingerprint=self._api_key_fingerprint(),
            user_id=self.secrets.get(ZERODHA_USER_ID) or "unknown",
            access_token=configured,
            issued_at=now,
            expires_at=web_session_expiry(now),
            session_date=session_day(now),
            kind=SESSION_KIND_ACCESS,
        )

    def _api_key_fingerprint(self) -> str:
        key = self.secrets.get(ZERODHA_API_KEY)
        return fingerprint(key) if key else ""

    # -- sign-in ------------------------------------------------------------ #

    def _login(self) -> TokenEntry:
        """Sign in the way the Kite website does: password, then TOTP."""

        user_id = self._secret(ZERODHA_USER_ID)
        password = self._secret(ZERODHA_PASSWORD)
        totp_secret = self._secret(ZERODHA_TOTP_SECRET)

        now = to_utc(self.clock())
        request_id = self._submit_password(user_id, password)
        self._submit_totp(user_id, request_id, totp_secret)

        token = self._session_cookie()
        if not token:
            raise BrokerAuthError(
                "the broker accepted the password and the TOTP code but returned "
                "no session cookie"
            )

        entry = TokenEntry(
            broker="zerodha",
            api_key_fingerprint=self._api_key_fingerprint(),
            user_id=user_id,
            access_token=token,
            issued_at=now,
            expires_at=web_session_expiry(now),
            session_date=session_day(now),
            kind=SESSION_KIND_WEB,
        )
        self._status.login_count += 1
        self._status.last_login_at = now
        if self.cache is not None:
            try:
                self.cache.store(entry)
            except OSError as exc:
                # A session that works is worth more than a cache that does
                # not; the next start simply signs in again.
                self._status.last_error = f"session cache not written: {type(exc).__name__}"
        return entry

    def _submit_password(self, user_id: str, password: str) -> str:
        response = self._request(
            "POST", KITE_LOGIN_URL, data={"user_id": user_id, "password": password}
        )
        payload = _payload(response, step="password")
        request_id = (payload.get("data") or {}).get("request_id")
        if not request_id:
            raise BrokerAuthError("the broker did not return a two-factor request id")
        return str(request_id)

    def _submit_totp(self, user_id: str, request_id: str, totp_secret: str) -> None:
        code = pyotp.TOTP(totp_secret).now()
        response = self._request(
            "POST",
            KITE_TWOFA_URL,
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": code,
                "twofa_type": "totp",
            },
        )
        payload = _payload(response, step="two-factor")
        if str(payload.get("status") or "").lower() != "success":
            message = str(payload.get("message") or "no reason given")[:200]
            raise BrokerAuthError(f"the broker refused the two-factor sign-in: {message}")

    def _session_cookie(self) -> str | None:
        client = self.client
        if client is None:
            return None
        value = client.cookies.get("enctoken")
        return str(value) if value else None

    def _secret(self, name: str) -> str:
        try:
            return self.secrets.require(name)
        except MissingSecret as exc:
            raise BrokerAuthError(
                f"{name} is not configured, so an unattended sign-in is impossible; "
                "set it in the environment or in FRAMEWORK_SECRETS_FILE"
            ) from exc

    # -- validation --------------------------------------------------------- #

    def _validates(self, entry: TokenEntry) -> bool:
        """One cheap call that proves the session still works."""

        client = self.client
        if client is None:  # pragma: no cover - the client is built in __post_init__
            return False
        url, headers = self._profile_request(entry)
        try:
            response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            self._status.last_error = f"broker unreachable: {type(exc).__name__}"
            return False
        if response.status_code == 200:
            return True
        self._status.last_error = f"the broker answered HTTP {response.status_code}"
        return False

    def _profile_request(self, entry: TokenEntry) -> tuple[str, dict[str, str]]:
        """Ask each session kind at the host that authenticates it.

        A web session authenticates the website's ``/oms`` API; a Kite Connect
        token authenticates the public API. Asking the wrong one reports a
        perfectly good session as dead.
        """

        if entry.kind == SESSION_KIND_WEB:
            return (
                KITE_WEB_PROFILE_URL,
                {"Authorization": f"enctoken {entry.access_token}", KITE_VERSION_HEADER[0]: KITE_VERSION_HEADER[1]},
            )
        api_key = self.secrets.get(ZERODHA_API_KEY) or ""
        return (
            KITE_PROFILE_URL,
            {
                "Authorization": f"token {api_key}:{entry.access_token}",
                KITE_VERSION_HEADER[0]: KITE_VERSION_HEADER[1],
            },
        )

    # -- transport ---------------------------------------------------------- #

    def _request(self, method: str, url: str, *, data: dict[str, str]) -> httpx.Response:
        client = self.client
        if client is None:  # pragma: no cover - the client is built in __post_init__
            raise BrokerAuthError("no HTTP client is configured")
        try:
            return client.request(method, url, data=data)
        except httpx.HTTPError as exc:
            message = f"the broker is unreachable: {type(exc).__name__}"
            self._status.last_error = message
            raise BrokerAuthError(message) from exc


def _payload(response: httpx.Response, *, step: str) -> dict[str, Any]:
    """Decode a Kite response, turning its own error into a clear exception."""

    try:
        payload = response.json()
    except ValueError as exc:
        raise BrokerAuthError(
            f"the broker's {step} step returned a non-JSON response "
            f"(HTTP {response.status_code})"
        ) from exc
    body = payload if isinstance(payload, dict) else {}
    status = str(body.get("status") or "").lower()
    if response.status_code >= 400 or status == "error":
        message = str(body.get("message") or f"HTTP {response.status_code}")[:200]
        error_type = str(body.get("error_type") or "unknown")
        raise BrokerAuthError(
            f"the broker rejected the {step} step ({error_type}): {message}"
        )
    return body

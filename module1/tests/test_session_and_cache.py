"""Unattended sign-in, and the session cache that keeps it rare.

The whole point of this module is that a person never appears in the loop, so
these tests drive the real code against a mocked broker: password, TOTP, the
session cookie, the cache write, and the second start-up that re-uses it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from module1.broker.session import BrokerAuthError, SessionManager
from module1.broker.token_cache import (
    TokenCache,
    TokenEntry,
    fingerprint,
    web_session_expiry,
)
from module1.secrets import SecretStore
from module1.timeutil import utc_now

pytestmark = pytest.mark.unit


def _secrets(tmp_path: Path, **overrides: str) -> SecretStore:
    values = {
        "ZERODHA_API_KEY": "kitekey",
        "ZERODHA_USER_ID": "AB1234",
        "ZERODHA_PASSWORD": "hunter2",
        "ZERODHA_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    }
    values.update(overrides)
    path = tmp_path / "secrets.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    return SecretStore(path)


def _broker(*, refuse_session: bool = False) -> tuple[httpx.Client, list[str]]:
    """A mocked Kite: the real routes, the real shapes, no network."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/api/login":
            return httpx.Response(200, json={"status": "success", "data": {"request_id": "rid-1"}})
        if path == "/api/twofa":
            return httpx.Response(
                200,
                json={"status": "success", "data": {}},
                headers={"set-cookie": "enctoken=session-token-value; Path=/"},
            )
        if path == "/oms/user/profile":
            if refuse_session:
                return httpx.Response(403, json={"status": "error", "message": "TokenException"})
            return httpx.Response(200, json={"status": "success", "data": {"user_id": "AB1234"}})
        return httpx.Response(404, json={"status": "error", "message": "Route not found"})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), seen


def test_sign_in_is_automatic_and_is_cached(tmp_path: Path) -> None:
    client, seen = _broker()
    cache = TokenCache(tmp_path / "token_cache.json")
    session = SessionManager(_secrets(tmp_path), cache=cache, client=client)

    held = session.ensure()

    assert held.token == "session-token-value"
    assert held.source == "login"
    assert "POST /api/login" in seen and "POST /api/twofa" in seen
    assert cache.load() is not None
    assert session.status()["login_count"] == 1
    assert session.status()["valid"] is True


def test_a_second_start_up_re_uses_the_cached_session(tmp_path: Path) -> None:
    cache = TokenCache(tmp_path / "token_cache.json")
    first_client, _ = _broker()
    first = SessionManager(_secrets(tmp_path), cache=cache, client=first_client)
    first.ensure()

    second_client, seen = _broker()
    second = SessionManager(_secrets(tmp_path), cache=cache, client=second_client)
    held = second.ensure()

    assert held.source == "cache"
    # No sign-in happened: the cached session was validated and used.
    assert "POST /api/login" not in seen
    assert second.status()["login_count"] == 0


def test_an_expired_cache_is_replaced_without_help(tmp_path: Path) -> None:
    cache = TokenCache(tmp_path / "token_cache.json")
    stale = TokenEntry(
        broker="zerodha",
        api_key_fingerprint="",
        user_id="AB1234",
        access_token="dead-token",
        issued_at=utc_now() - timedelta(days=60),
        expires_at=utc_now() - timedelta(days=30),
        session_date=(utc_now() - timedelta(days=60)).date(),
    )
    cache.store(stale)

    client, seen = _broker()
    session = SessionManager(_secrets(tmp_path), cache=cache, client=client)
    held = session.ensure()

    assert held.token == "session-token-value"
    assert "POST /api/login" in seen


def test_a_refused_session_is_rebuilt_from_the_cache_first(tmp_path: Path) -> None:
    """Another process may already have signed in; adopt it, do not replace it."""

    cache = TokenCache(tmp_path / "token_cache.json")
    cache.store(
        TokenEntry(
            broker="zerodha",
            api_key_fingerprint=fingerprint("kitekey"),
            user_id="AB1234",
            access_token="fresher-token",
            issued_at=utc_now(),
            expires_at=web_session_expiry(),
            session_date=utc_now().date(),
        )
    )
    client, seen = _broker()
    session = SessionManager(_secrets(tmp_path), cache=cache, client=client)

    held = session.heal()

    assert held.token == "fresher-token"
    assert "POST /api/login" not in seen


def test_a_bad_two_factor_answer_is_reported_without_a_secret(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"status": "success", "data": {"request_id": "rid"}})
        return httpx.Response(200, json={"status": "error", "message": "Invalid TOTP"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    session = SessionManager(
        _secrets(tmp_path), cache=TokenCache(tmp_path / "token_cache.json"), client=client
    )

    with pytest.raises(BrokerAuthError) as caught:
        session.ensure()

    message = str(caught.value)
    assert "Invalid TOTP" in message
    assert "hunter2" not in message
    assert "JBSWY3DPEHPK3PXP" not in message


def test_the_cache_refuses_to_live_inside_the_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="outside the repository"):
        TokenCache(root / "token_cache.json", project_root=root)

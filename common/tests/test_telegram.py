"""The notifier: useful when it works, invisible when it does not."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from common.telegram import (
    MAX_MESSAGE_CHARS,
    NullNotifier,
    TelegramNotifier,
    escape,
    notifier_from_env,
)

pytestmark = pytest.mark.unit


class FakeTelegram:
    """An httpx.Client stand-in that records what would have been sent."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.calls.append({"url": url, "json": json})
        return httpx.Response(
            self.status_code,
            json={"ok": self.status_code < 400},
            request=httpx.Request("POST", f"https://api.telegram.org{url}"),
        )

    def close(self) -> None:
        return None


@pytest.fixture
def fake() -> FakeTelegram:
    return FakeTelegram()


def notifier(fake: FakeTelegram, **overrides: Any) -> TelegramNotifier:
    return TelegramNotifier(
        "token-123",
        "chat-456",
        client=fake,  # type: ignore[arg-type]
        dedupe_seconds=overrides.pop("dedupe_seconds", 60.0),
        **overrides,
    )


def test_a_message_goes_to_the_configured_chat(fake: FakeTelegram) -> None:
    sender = notifier(fake)
    try:
        sent, error = sender.send_now("<b>hello</b>")
    finally:
        sender.close()

    assert sent is True and error is None
    assert fake.calls[0]["url"] == "/bottoken-123/sendMessage"
    assert fake.calls[0]["json"]["chat_id"] == "chat-456"
    assert fake.calls[0]["json"]["text"] == "<b>hello</b>"


def test_a_refusal_is_counted_and_never_raised(fake: FakeTelegram) -> None:
    fake.status_code = 500
    sender = notifier(fake)
    try:
        sent, error = sender.send_now("hello")
    finally:
        sender.close()

    assert sent is False
    assert "HTTP 500" in (error or "")
    assert sender.failed == 1
    assert "token-123" not in (sender.describe()["last_error"] or "")


def test_the_same_key_is_not_repeated_inside_the_dedupe_window(fake: FakeTelegram) -> None:
    clock = [0.0]
    sender = notifier(fake, clock=lambda: clock[0], dedupe_seconds=60.0)
    try:
        assert sender.notify("first", key="module.feed") is True
        clock[0] = 10.0
        assert sender.notify("again", key="module.feed") is False
        clock[0] = 61.0
        assert sender.notify("after the window", key="module.feed") is True
    finally:
        sender.close()

    assert sender.dropped == 1


def test_a_long_message_is_clipped_rather_than_refused(fake: FakeTelegram) -> None:
    sender = notifier(fake)
    try:
        sent, _ = sender.send_now("x" * 10_000)
    finally:
        sender.close()

    assert sent is True
    text = fake.calls[0]["json"]["text"]
    assert len(text) == MAX_MESSAGE_CHARS
    assert text.endswith("(truncated)")


def test_html_in_the_message_is_escaped_by_the_caller_helper() -> None:
    assert escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def test_no_token_means_a_silent_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("common.telegram.env.secret", lambda _name: None)

    sender = notifier_from_env("test")

    assert isinstance(sender, NullNotifier)
    assert sender.notify("anything") is False
    assert sender.describe()["enabled"] is False
    assert "TELEGRAM_BOT_TOKEN" in sender.describe()["reason"]


def test_the_notifier_can_be_muted_while_keeping_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")

    sender = notifier_from_env("test")

    assert isinstance(sender, NullNotifier)
    assert "TELEGRAM_ENABLED" in sender.describe()["reason"]


def test_a_notifier_is_built_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "from-env-chat")

    sender = notifier_from_env("test")
    try:
        assert isinstance(sender, TelegramNotifier)
        assert sender.describe()["enabled"] is True
        # The token never appears in what a status panel can show.
        assert "from-env-token" not in str(sender.describe())
    finally:
        sender.close()

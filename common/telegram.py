"""The one way this framework talks to its operator.

A trading system must never be blocked by its own paperwork, so every rule
here is about what a notification *cannot* do:

* it cannot block a caller: ``notify`` puts the message on a bounded queue and
  returns immediately, and a full queue drops the oldest waiting message
  rather than a trading cycle;
* it cannot raise: a failed send is logged and counted, never thrown back at
  the module that produced it;
* it cannot flood: a message sent with a ``key`` is suppressed for a dedupe
  window, so a reconnect loop becomes one message instead of a hundred;
* it cannot leak: the bot token appears only in the URL of the request this
  module itself makes, and never in a log line, a status payload or an error.

The notifier is disabled, quietly, when no token and chat id are configured -
a developer running the framework locally should not need a Telegram bot.
"""

from __future__ import annotations

import html
import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Final, Protocol

import httpx

from common import env

TELEGRAM_BOT_TOKEN: Final[str] = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID: Final[str] = "TELEGRAM_CHAT_ID"
TELEGRAM_ENABLED: Final[str] = "TELEGRAM_ENABLED"

TELEGRAM_API_URL: Final[str] = "https://api.telegram.org"
#: Telegram's own ceiling is 4096 characters; leave room for the marker.
MAX_MESSAGE_CHARS: Final[int] = 3900
DEFAULT_DEDUPE_SECONDS: Final[float] = 60.0
DEFAULT_QUEUE_SIZE: Final[int] = 500
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0

logger = logging.getLogger("common.telegram")


class Notifier(Protocol):
    """What a module asks for: say this, or do not, but never fail me."""

    def notify(
        self, text: str, *, key: str | None = None, dedupe_seconds: float | None = None
    ) -> bool: ...

    def describe(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class NullNotifier:
    """A notifier that says nothing, for a process with no bot configured."""

    def __init__(self, reason: str = "not configured") -> None:
        self.reason = reason
        self.dropped = 0

    def notify(
        self, text: str, *, key: str | None = None, dedupe_seconds: float | None = None
    ) -> bool:
        self.dropped += 1
        return False

    def send_now(self, text: str) -> tuple[bool, str | None]:
        return False, self.reason

    def flush(self, timeout: float = 0.0) -> None:
        return None

    def describe(self) -> dict[str, Any]:
        return {"enabled": False, "reason": self.reason, "dropped": self.dropped}

    def close(self) -> None:
        return None


class TelegramNotifier:
    """A queued, throttled, never-raising Telegram sender."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        name: str = "framework",
        client: httpx.Client | None = None,
        dedupe_seconds: float = DEFAULT_DEDUPE_SECONDS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._name = name
        self._client = client or httpx.Client(
            base_url=TELEGRAM_API_URL, timeout=timeout_seconds
        )
        self._owns_client = client is None
        self._dedupe_seconds = max(0.0, float(dedupe_seconds))
        self._clock = clock
        self._sleep = sleeper
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"telegram-{name}", daemon=True
        )
        self._last_sent: dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error: str | None = None
        self.last_sent_at: float | None = None
        self._thread.start()

    # -- public surface ----------------------------------------------------- #

    def notify(
        self, text: str, *, key: str | None = None, dedupe_seconds: float | None = None
    ) -> bool:
        """Queue one message. ``True`` means it was accepted, not delivered."""

        body = (text or "").strip()
        if not body:
            return False
        window = self._dedupe_seconds if dedupe_seconds is None else max(0.0, dedupe_seconds)
        if key and window > 0:
            last = self._last_sent.get(key)
            if last is not None and self._clock() - last < window:
                self.dropped += 1
                return False
            self._last_sent[key] = self._clock()
        try:
            self._queue.put_nowait((_clip(body), key))
        except queue.Full:
            # The queue is a convenience, never a place a signal hides behind:
            # dropping the *oldest* waiting message keeps the newest facts.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((_clip(body), key))
            except (queue.Empty, queue.Full):  # pragma: no cover - both at once
                self.dropped += 1
                return False
            self.dropped += 1
        return True

    def send_now(self, text: str) -> tuple[bool, str | None]:
        """Send one message synchronously: for ``--check`` and for tests."""

        ok, error = self._send(_clip((text or "").strip()))
        return ok, error

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the queue to drain, best effort."""

        deadline = time.monotonic() + max(0.0, timeout)
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    def close(self) -> None:
        self._stop.set()
        with suppress(queue.Full):
            self._queue.put_nowait(("", None))
        thread = self._thread
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        if self._owns_client:
            self._client.close()

    def describe(self) -> dict[str, Any]:
        """Status for the module's own status panel. Never the token."""

        return {
            "enabled": True,
            "name": self._name,
            "queued": self._queue.qsize(),
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
        }

    # -- worker -------------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text, _key = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not text:
                continue
            self._send(text)

    def _send(self, text: str) -> tuple[bool, str | None]:
        if not text:
            return False, "nothing to send"
        for attempt in (1, 2):
            try:
                response = self._client.post(
                    f"/bot{self._token}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            except httpx.HTTPError as exc:
                self._failed(f"telegram unreachable: {type(exc).__name__}")
                if attempt == 1:
                    self._sleep(1.0)
                    continue
                return False, self.last_error
            if response.status_code == 429 and attempt == 1:
                # Telegram says how long to wait; respect it once.
                self._sleep(min(5.0, _retry_after(response)))
                continue
            if response.status_code >= 400:
                self._failed(f"telegram refused the message: HTTP {response.status_code}")
                return False, self.last_error
            self.sent += 1
            self.last_sent_at = self._clock()
            self.last_error = None
            return True, None
        return False, self.last_error

    def _failed(self, message: str) -> None:
        self.failed += 1
        self.last_error = message[:200]
        logger.warning("telegram: %s", self.last_error)


def notifier_from_env(name: str = "framework") -> Notifier:
    """Build the notifier this process should use, or the silent one."""

    if not env.flag(TELEGRAM_ENABLED, True):
        return NullNotifier("disabled by TELEGRAM_ENABLED")
    token = env.secret(TELEGRAM_BOT_TOKEN)
    chat_id = env.secret(TELEGRAM_CHAT_ID)
    if not token or not chat_id:
        missing = [
            symbol
            for symbol, value in ((TELEGRAM_BOT_TOKEN, token), (TELEGRAM_CHAT_ID, chat_id))
            if not value
        ]
        return NullNotifier(f"no {' or '.join(missing)} configured")
    return TelegramNotifier(token, chat_id, name=name)


def escape(value: object) -> str:
    """Text for an HTML-parsed Telegram message, with nothing left to parse."""

    return html.escape(str(value if value is not None else ""), quote=False)


def _clip(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    marker = "\n… (truncated)"
    return text[: MAX_MESSAGE_CHARS - len(marker)] + marker


def _retry_after(response: httpx.Response) -> float:
    try:
        payload = response.json()
    except ValueError:
        return 1.0
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    if isinstance(parameters, dict):
        try:
            return float(parameters.get("retry_after") or 1.0)
        except (TypeError, ValueError):
            return 1.0
    return 1.0


__all__ = [
    "MAX_MESSAGE_CHARS",
    "Notifier",
    "NullNotifier",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ENABLED",
    "TelegramNotifier",
    "escape",
    "notifier_from_env",
]

"""The broker's request budget, shared by everything that spends it.

There is exactly one rate limit here, and it belongs to the account, not to a
connection: opening more sockets does not buy more requests. So the limiter is
a single token bucket that every caller draws from.

What it adds over a plain sleep-between-calls gate is *priority*. A warm-up or
a repair pass can be a few hundred requests, and without lanes a person
clicking a symbol waits behind all of them. Callers that a person is waiting on
take the next token; background work uses whatever is left over.

It says nothing about concurrency. Callers may run in parallel threads; the
bucket only decides *when* each request may start, which is the one thing the
broker actually counts.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

#: How long a waiter sleeps before re-checking the bucket. Small enough to be
#: responsive at any realistic rate, large enough to cost nothing.
_POLL_SECONDS = 0.02


class RateLimiter:
    """A token bucket with two lanes: interactive and background."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = max(0.001, float(rate_per_second))
        self._capacity = float(burst if burst is not None else max(1.0, self._rate))
        self._tokens = self._capacity
        self._updated = clock()
        self._clock = clock
        self._condition = threading.Condition()
        self._interactive_waiting = 0
        self.granted = 0
        self.interactive_granted = 0
        self.total_wait_seconds = 0.0

    def acquire(self, *, interactive: bool = False) -> float:
        """Wait for one request's worth of budget and return how long it took."""

        started = self._clock()
        with self._condition:
            if interactive:
                self._interactive_waiting += 1
            try:
                while True:
                    self._refill()
                    # Background work yields whenever a person is waiting.
                    allowed = interactive or self._interactive_waiting == 0
                    if self._tokens >= 1.0 and allowed:
                        self._tokens -= 1.0
                        self.granted += 1
                        if interactive:
                            self.interactive_granted += 1
                        waited = self._clock() - started
                        self.total_wait_seconds += waited
                        return waited
                    self._condition.wait(timeout=_POLL_SECONDS)
            finally:
                if interactive:
                    self._interactive_waiting -= 1
                    self._condition.notify_all()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    @property
    def tokens(self) -> float:
        with self._condition:
            self._refill()
            return self._tokens

    def describe(self) -> dict[str, Any]:
        with self._condition:
            self._refill()
            return {
                "rate_per_second": round(self._rate, 3),
                "burst": self._capacity,
                "tokens": round(self._tokens, 2),
                "granted": self.granted,
                "interactive_granted": self.interactive_granted,
                "interactive_waiting": self._interactive_waiting,
                "total_wait_seconds": round(self.total_wait_seconds, 2),
            }


def unlimited() -> RateLimiter:
    """A limiter too fast to matter, for tests and for a disabled throttle."""

    return RateLimiter(rate_per_second=1_000_000.0, burst=1_000_000.0)

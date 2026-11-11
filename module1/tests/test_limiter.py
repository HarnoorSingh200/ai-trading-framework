"""One budget for the account, spent in priority order."""

from __future__ import annotations

import threading
import time

import pytest

from module1.limiter import RateLimiter, unlimited

pytestmark = pytest.mark.unit


def test_a_burst_is_allowed_then_the_rate_paces_everything_after() -> None:
    limiter = RateLimiter(rate_per_second=20.0, burst=3)

    started = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    burst_seconds = time.monotonic() - started

    # A bucket of three empties without waiting.
    assert burst_seconds < 0.1
    limiter.acquire()
    assert time.monotonic() - started >= 0.03


def test_the_bucket_never_exceeds_its_capacity() -> None:
    limiter = RateLimiter(rate_per_second=1000.0, burst=2)

    time.sleep(0.05)

    assert limiter.tokens <= 2.0


def test_a_person_waiting_jumps_the_background_queue() -> None:
    """Background work must yield to a request someone is waiting on."""

    limiter = RateLimiter(rate_per_second=10.0, burst=1)
    limiter.acquire()  # empty the bucket

    order: list[tuple[str, float]] = []
    started = time.monotonic()

    def background() -> None:
        limiter.acquire(interactive=False)
        order.append(("background", time.monotonic() - started))

    worker = threading.Thread(target=background)
    worker.start()
    time.sleep(0.05)  # let the background caller reach the front of the queue

    limiter.acquire(interactive=True)
    order.append(("interactive", time.monotonic() - started))
    worker.join(timeout=2)

    assert [name for name, _ in order] == ["interactive", "background"]
    assert limiter.describe()["interactive_granted"] == 1


def test_the_rate_is_reported_for_the_status_panel() -> None:
    limiter = RateLimiter(rate_per_second=2.85, burst=3.0)

    described = limiter.describe()

    assert described["rate_per_second"] == pytest.approx(2.85)
    assert described["burst"] == 3.0
    assert described["granted"] == 0


def test_the_unlimited_limiter_hardly_waits() -> None:
    limiter = unlimited()

    started = time.monotonic()
    for _ in range(50):
        limiter.acquire(interactive=True)

    assert time.monotonic() - started < 0.1

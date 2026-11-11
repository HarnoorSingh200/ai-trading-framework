"""Historical requests, and the per-day cap that hides the end of a session.

Kite returns at most 360 candles for any single calendar day, taking the
*earliest* of the range it was asked for. An NSE session is 375 minutes, so the
last fifteen are missing from a one-day request - and the follow-up cannot say
"same day, later" with a date-only bound, because it sends the same date and
gets the same answer back.

These tests run the real client against a mocked broker that enforces the cap,
so the fix is proven rather than assumed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from module1.broker.kite import MAX_CANDLES_PER_DAY, KiteClient
from module1.timeutil import IST

pytestmark = pytest.mark.unit

SESSION_START = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
#: A full NSE equity session: 375 one-minute candles.
SESSION_MINUTES = 375
BASE_URL = "https://kite.zerodha.com"


class FakeSession:
    """The only part of a session the client uses."""

    def token(self) -> str:
        return "session-token"

    def heal(self) -> None:
        return None


def _session_minutes(count: int = SESSION_MINUTES) -> list[datetime]:
    return [SESSION_START + timedelta(minutes=index) for index in range(count)]


def _transport(handler) -> httpx.Client:
    """A client whose base URL lets the client's relative paths resolve."""

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)


def _broker(
    *, session_minutes: int = SESSION_MINUTES
) -> tuple[httpx.Client, list[dict[str, str]]]:
    """A mocked Kite that applies the real per-day cap.

    ``session_minutes`` is how much of the session the broker actually holds.
    The live broker holds 360 of the 375 - exactly the cap - which is what
    makes the tail probe come back empty in practice.
    """

    requested: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = {key: str(value) for key, value in request.url.params.items()}
        requested.append(params)
        start = datetime.strptime(params["from"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        end = datetime.strptime(params["to"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)

        wanted = [
            moment for moment in _session_minutes(session_minutes) if start <= moment <= end
        ]
        capped = wanted[:MAX_CANDLES_PER_DAY]
        rows = [
            [moment.strftime("%Y-%m-%dT%H:%M:%S+0530"), 100, 101, 99, 100.5, 1000]
            for moment in capped
        ]
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    return _transport(handler), requested


def _client(transport: httpx.Client) -> KiteClient:
    return KiteClient(
        FakeSession(),  # type: ignore[arg-type]
        client=transport,
        rate_limit_seconds=0.0,
    )


def test_the_whole_session_survives_the_per_day_cap() -> None:
    transport, requested = _broker()
    client = _client(transport)

    candles = client.historical(
        "3861249",
        "1m",
        start=SESSION_START,
        end=SESSION_START + timedelta(hours=8),
    )

    assert len(candles) == SESSION_MINUTES
    assert candles[0].open_time == SESSION_START
    assert candles[-1].open_time == SESSION_START + timedelta(minutes=SESSION_MINUTES - 1)
    # The cap was hit, so a second request asked for the rest of the same day.
    assert len(requested) == 2


def test_the_follow_up_names_a_time_of_day() -> None:
    """A date-only bound cannot advance inside a day, which was the whole bug."""

    transport, requested = _broker()
    client = _client(transport)

    client.historical(
        "3861249", "1m", start=SESSION_START, end=SESSION_START + timedelta(hours=8)
    )

    first, second = requested
    assert first["from"] == "2026-09-21 09:15:00"
    # The second request starts exactly where the first answer stopped.
    assert second["from"] == "2026-09-21 15:15:00"
    for params in requested:
        assert "T" not in params["from"] and "Z" not in params["from"]


def test_a_short_day_costs_only_one_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rows = [
            [
                (SESSION_START + timedelta(minutes=index)).strftime(
                    "%Y-%m-%dT%H:%M:%S+0530"
                ),
                100,
                101,
                99,
                100.5,
                1000,
            ]
            for index in range(100)
        ]
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    calls: list[Any] = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params)
        return handler(request)

    client = _client(_transport(counting))

    candles = client.historical(
        "3861249", "1m", start=SESSION_START, end=SESSION_START + timedelta(hours=8)
    )

    assert len(candles) == 100
    assert len(calls) == 1


def test_the_tail_is_probed_once_per_day_not_once_per_symbol() -> None:
    """Fifty symbols at the cap must not ask the same question fifty times."""

    # The real shape: the session ends exactly where the cap does.
    transport, requested = _broker(session_minutes=MAX_CANDLES_PER_DAY)
    client = _client(transport)

    for token in ("111", "222", "333"):
        client.historical(
            token, "1m", start=SESSION_START, end=SESSION_START + timedelta(hours=8)
        )

    # One range request each, and a tail probe only on the first.
    assert len(requested) == 4
    assert client.status()["tail_probes_skipped"] == 2


def test_a_day_that_did_have_a_tail_is_always_asked_again() -> None:
    """The memory is of 'nothing there', never of 'do not look'."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = {key: str(value) for key, value in request.url.params.items()}
        start = datetime.strptime(params["from"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        if start.hour >= 15 and start.minute >= 15:
            # There is more of this session after the cap.
            rows = [
                [
                    (SESSION_START + timedelta(minutes=index)).strftime(
                        "%Y-%m-%dT%H:%M:%S+0530"
                    ),
                    100,
                    101,
                    99,
                    100.5,
                    1000,
                ]
                for index in range(360, SESSION_MINUTES)
            ]
            return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})
        rows = [
            [
                (SESSION_START + timedelta(minutes=index)).strftime(
                    "%Y-%m-%dT%H:%M:%S+0530"
                ),
                100,
                101,
                99,
                100.5,
                1000,
            ]
            for index in range(MAX_CANDLES_PER_DAY)
        ]
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    client = _client(_transport(handler))

    first = client.historical(
        "111", "1m", start=SESSION_START, end=SESSION_START + timedelta(hours=12)
    )
    second = client.historical(
        "222", "1m", start=SESSION_START, end=SESSION_START + timedelta(hours=12)
    )

    assert len(first) == SESSION_MINUTES
    assert len(second) == SESSION_MINUTES
    assert client.status()["tail_probes_skipped"] == 0


def test_an_unknown_interval_is_refused_before_any_request() -> None:
    transport, requested = _broker()
    client = _client(transport)

    with pytest.raises(Exception, match="unsupported interval"):
        client.historical("3861249", "7m", start=SESSION_START, end=SESSION_START)

    assert requested == []

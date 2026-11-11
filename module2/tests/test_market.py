"""Module 1, as Module 2 sees it."""

from __future__ import annotations

import json

import httpx
import pytest

from module2.market import MarketClient, MarketError

pytestmark = pytest.mark.unit


def client(handler) -> MarketClient:  # type: ignore[no-untyped-def]
    return MarketClient(
        "http://127.0.0.1:5201",
        client=httpx.Client(
            base_url="http://127.0.0.1:5201", transport=httpx.MockTransport(handler)
        ),
    )


def test_a_screen_is_posted_and_its_data_unwrapped() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"interval": "15m", "rows": [{"symbol": "INFY"}]},
            },
        )

    result = client(handler).screen(
        interval="15m", where=[{"field": "rsi_14", "op": "<", "value": 30}], limit=5
    )

    assert seen["path"] == "/v1/market/screen"
    assert seen["body"]["interval"] == "15m"
    assert seen["body"]["limit"] == 5
    assert result["rows"][0]["symbol"] == "INFY"


def test_one_stock_is_fetched_on_one_timeframe() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = str(request.url.query.decode())
        return httpx.Response(200, json={"ok": True, "data": {"symbol": "INFY"}})

    client(handler).stock("INFY", "15m")

    assert seen["path"] == "/v1/market/symbol/INFY"
    assert "interval=15m" in seen["query"]


def test_a_refusal_from_module_1_keeps_its_own_message() -> None:
    """The reason the screen was refused is the reason the agent must see."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error": {
                    "code": "SCREEN_INVALID",
                    "message": "'rsi' is not a field this module publishes",
                    "details": {"field": "rsi"},
                },
            },
        )

    with pytest.raises(MarketError) as failure:
        client(handler).screen(interval="15m", where=[])

    assert failure.value.code == "SCREEN_INVALID"
    assert "not a field" in str(failure.value)
    assert failure.value.status_code == 400


def test_an_unreachable_module_1_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(MarketError) as failure:
        client(handler).fields("15m")

    assert "unreachable" in str(failure.value).lower()


def test_the_bar_that_just_closed_is_asked_about_directly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/market/bar-status"
        assert request.url.params["interval"] == "15m"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "interval": "15m",
                    "bar_open_time": "2026-09-21T10:30:00+05:30",
                    "bar_close_time": "2026-09-21T10:45:00+05:30",
                    "symbols": 50,
                    "ready": 48,
                    "building": 2,
                    "waiting": 0,
                    "complete": False,
                },
            },
        )

    payload = client(handler).bar_status("15m")

    assert payload["bar_open_time"] == "2026-09-21T10:30:00+05:30"
    assert payload["ready"] == 48
    assert payload["complete"] is False


def test_the_session_state_is_gathered_into_one_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "board": {
                        "session_clock": {"is_open": True, "phase": "open"},
                        "feed": {"connected": True, "ticks": 10},
                        "board": {"history_ready": True},
                    },
                    "candles": {"book": {"minutes_held": 100}},
                },
            },
        )

    payload = client(handler).market_state()

    assert payload["session"]["is_open"] is True
    assert payload["feed"]["ticks"] == 10
    assert payload["candles"]["minutes_held"] == 100

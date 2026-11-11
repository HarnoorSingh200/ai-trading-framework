"""Talking to the two neighbours: the price stream, and Module 2's log."""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from module3.clients import (
    DecisionsClient,
    MarketClient,
    MarketError,
    PriceStream,
    parse_sse,
    unwrap,
)

pytestmark = pytest.mark.unit


def test_parse_sse_reads_events_and_ignores_keepalives() -> None:
    lines = [
        ": keepalive",
        "event: prices",
        'data: {"rows": [1, 2]}',
        "",
        "event: prices",
        "data: not json",
        "",
        "event: prices",
        'data: {"rows": [3]}',
        "",
    ]

    events = list(parse_sse(lines))

    assert [event["data"] for event in events] == [
        {"rows": [1, 2]},
        {"rows": [3]},
    ]


def test_a_refusal_keeps_the_words_of_the_module_that_made_it() -> None:
    request = httpx.Request("GET", "http://module1/v1/market/prices")
    response = httpx.Response(
        400,
        json={
            "detail": {
                "ok": False,
                "error": {"code": "SYMBOLS_REQUIRED", "message": "name at least one symbol"},
            }
        },
        request=request,
    )

    with pytest.raises(MarketError) as caught:
        unwrap(response)

    assert caught.value.code == "SYMBOLS_REQUIRED"
    assert "name at least one symbol" in str(caught.value)


def test_a_successful_answer_is_unwrapped_from_its_envelope() -> None:
    request = httpx.Request("GET", "http://module1/v1/market/prices")
    response = httpx.Response(
        200,
        json={"ok": True, "data": {"rows": [{"symbol": "TCS"}]}},
        request=request,
    )

    assert unwrap(response) == {"rows": [{"symbol": "TCS"}]}


def test_the_price_read_names_its_symbols() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params.get("symbols")))
        return httpx.Response(200, json={"ok": True, "data": {"rows": []}})

    client = MarketClient(
        "http://module1",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://module1"
        ),
    )

    client.prices(["TCS", "INFY"])

    assert seen == ["TCS,INFY"]


def test_the_price_stream_delivers_what_module_1_pushes() -> None:
    seen: list[dict[str, Any]] = []
    stop = threading.Event()
    body = (
        "event: prices\n"
        'data: {"rows": [{"symbol": "TCS", "last_price": 412.9, "price_source": "live"}]}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://module1"
    )
    stream = PriceStream(
        "http://module1",
        symbols=lambda: ["TCS"],
        on_prices=lambda rows: (seen.extend(rows), stop.set()),
        client=client,
    )

    thread = threading.Thread(target=stream.run, args=(stop,), daemon=True)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert seen == [{"symbol": "TCS", "last_price": 412.9, "price_source": "live"}]
    assert stream.status()["frames"] == 1
    assert stream.status()["watching"] == 1
    client.close()


def test_the_price_stream_stops_when_there_is_nothing_to_watch() -> None:
    opened = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal opened
        opened = True
        return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://module1"
    )
    stop = threading.Event()
    stop.set()
    stream = PriceStream(
        "http://module1", symbols=lambda: [], on_prices=lambda rows: None, client=client
    )

    stream.run(stop)

    # Nothing is held, so no subscription was ever opened on Module 1.
    assert opened is False
    assert stream.status()["state"] == "stopped"
    client.close()


def test_the_decisions_read_finds_agents_and_what_they_decided() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"agents": [{"agent_id": "alpha"}, {"agent_id": "beta"}]}},
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"decisions": [{"decision_id": "dec_1", "action": "buy"}]},
            },
        )

    directory = DecisionsClient(
        "http://module2",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://module2"
        ),
    )

    assert directory.agents() == ["alpha", "beta"]
    assert directory.decisions("alpha")[0]["decision_id"] == "dec_1"


def test_an_unreachable_neighbour_is_an_error_and_not_an_empty_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    directory = DecisionsClient(
        "http://module2",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://module2"
        ),
    )

    with pytest.raises(MarketError) as caught:
        directory.agents()

    assert "Module 2 is unreachable" in str(caught.value)

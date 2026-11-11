"""Handing a decision to Module 3: what is sent, and what is done when it fails."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from module2.orders import OrderBookError, OrderForwarder, OrderReader

pytestmark = pytest.mark.unit

DECISION: dict[str, Any] = {
    "decision_id": "dec_abc",
    "agent_id": "alpha",
    "action": "buy",
    "symbol": "TCS",
    "quantity": 96,
    "entry_price": 412.9,
    "target": 416.5,
    "stop_loss": 411.4,
    "max_hold_minutes": 60,
    "reason": "close above VWAP",
}


def forwarder(handler: Any) -> tuple[OrderForwarder, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://module3"
    )
    return OrderForwarder("http://module3", client=client), client


def reader(handler: Any) -> tuple[OrderReader, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://module3"
    )
    return OrderReader("http://module3", client=client), client


def test_a_decision_is_posted_to_the_order_book() -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={"ok": True, "data": {"stored": True}})

    held, client = forwarder(handler)

    assert held.deliver(DECISION) is True

    assert seen[0][0] == "/v1/orders"
    assert b"dec_abc" in seen[0][1]
    assert held.delivered == 1
    assert held.failures == 0
    assert held.last_error is None
    client.close()


def test_a_refusal_keeps_module_3_s_own_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": {
                    "ok": False,
                    "error": {
                        "code": "ORDER_INVALID",
                        "message": "a buy needs the stop below the entry",
                    },
                }
            },
        )

    held, client = forwarder(handler)

    assert held.deliver(DECISION) is False

    assert held.failures == 1
    assert "stop below the entry" in str(held.last_error)
    client.close()


def test_an_unreachable_module_3_is_reported_and_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing is listening", request=request)

    held, client = forwarder(handler)

    assert held.deliver(DECISION) is False

    assert held.failures == 1
    assert "ConnectError" in str(held.last_error)
    client.close()


def test_the_forwarder_says_what_it_is_for() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={"ok": True, "data": {}})

    held, client = forwarder(handler)
    held.deliver(DECISION)

    described = held.describe()

    assert described["delivered"] == 1
    assert described["base_url"] == "http://module3"
    # The reason a failed hand-over is not a lost order, stated where an
    # operator reading the status panel will find it.
    assert "delays an order rather than losing it" in described["note"]
    client.close()


# -- reading the agent's own book ------------------------------------------- #


def test_the_reader_asks_for_one_agent_s_orders() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"count": 1, "orders": [{"symbol": "TCS", "status": "open"}]},
            },
        )

    held, client = reader(handler)

    payload = held.mine("alpha", limit=5)

    # The agent it is scoped to travels as a query parameter, which is what
    # Module 3 filters on.
    assert seen[0] == {"agent_id": "alpha", "limit": "5"}
    assert payload["orders"][0]["symbol"] == "TCS"
    assert held.calls == 1
    client.close()


def test_the_reader_can_narrow_to_one_state() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"ok": True, "data": {"count": 0, "orders": []}})

    held, client = reader(handler)

    held.mine("alpha", status="closed")

    assert seen[0]["status"] == "closed"
    client.close()


def test_an_unreachable_book_says_so_rather_than_returning_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing is listening", request=request)

    held, client = reader(handler)

    with pytest.raises(OrderBookError) as caught:
        held.mine("alpha")

    # An empty book and an unreadable one are not the same answer, and the model
    # has to be able to tell them apart.
    assert "unreachable" in str(caught.value)
    assert held.failures == 1
    client.close()


def test_a_refusal_from_the_book_keeps_its_own_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": {
                    "ok": False,
                    "error": {"code": "ORDER_QUERY_INVALID", "message": "unknown status"},
                }
            },
        )

    held, client = reader(handler)

    with pytest.raises(OrderBookError) as caught:
        held.mine("alpha", status="nonsense")

    assert "unknown status" in str(caught.value)
    client.close()

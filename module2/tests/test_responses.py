"""The Responses transport: tools plus reasoning, stateless and replayable."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from module2.responses import ResponsesClient

pytestmark = pytest.mark.unit

KEY = "sk-test-not-a-real-key"


def client(handler, **overrides) -> ResponsesClient:  # type: ignore[no-untyped-def]
    return ResponsesClient(
        base_url="https://api.example.test/v1",
        api_key=KEY,
        model="gpt-6-luna",
        transport=httpx.MockTransport(handler),
        **overrides,
    )


def reply(found: ResponsesClient, *, messages=None, tools=None):  # type: ignore[no-untyped-def]
    return asyncio.run(
        found.acomplete(
            messages=messages or [{"role": "user", "content": "decide"}],
            tools=tools or [],
            temperature=0.2,
            max_tokens=500,
            timeout=5.0,
        )
    )


def message_output(text: str = "buy INFY") -> list[dict]:
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    ]


def test_the_request_is_responses_shaped_and_stateless() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "gpt-6-luna", "status": "completed", "output": message_output()},
        )

    found = reply(
        client(handler),
        messages=[
            {"role": "system", "content": "You trade the tape."},
            {"role": "user", "content": "decide"},
        ],
    )

    body = seen["body"]
    assert found.ok is True
    assert seen["path"] == "/v1/responses"
    assert body["model"] == "gpt-6-luna"
    assert body["instructions"] == "You trade the tape."
    assert body["input"] == [{"role": "user", "content": "decide"}]
    assert body["store"] is False
    assert body["max_output_tokens"] == 500
    assert body["reasoning"] == {"effort": "medium"}
    # A reasoning model does not take sampling parameters.
    assert "temperature" not in body


def test_tool_schemas_are_flattened_for_the_responses_api() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"status": "completed", "output": message_output()},
        )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "screen",
                "description": "Filter the universe.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    reply(client(handler), tools=tools)

    assert seen["body"]["tools"] == [
        {
            "type": "function",
            "name": "screen",
            "description": "Filter the universe.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_text_tool_calls_reasoning_items_and_usage_are_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-6-luna",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "cipher"},
                    *message_output("buy INFY"),
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "screen",
                        "arguments": json.dumps(
                            {"where": [{"field": "rsi_14", "op": "<", "value": 30}]}
                        ),
                    },
                ],
                "usage": {
                    "input_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 100},
                    "output_tokens": 40,
                    "output_tokens_details": {"reasoning_tokens": 25},
                },
            },
        )

    found = reply(client(handler))

    assert found.ok is True
    assert found.text == "buy INFY"
    assert found.tool_calls[0].name == "screen"
    assert found.tool_calls[0].call_id == "call_1"
    assert found.tool_calls[0].arguments["where"][0]["field"] == "rsi_14"
    assert [item["type"] for item in found.provider_items] == [
        "reasoning",
        "message",
        "function_call",
    ]
    assert found.usage["prompt_tokens"] == 120
    assert found.usage["completion_tokens"] == 40
    assert found.usage["prompt_cache_hit_tokens"] == 100
    assert found.usage["prompt_cache_miss_tokens"] == 20
    assert found.usage["reasoning_tokens"] == 25


def test_reasoning_items_and_tool_results_are_replayed_in_order() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "completed", "output": message_output()})

    messages = [
        {"role": "system", "content": "frame"},
        {"role": "user", "content": "decide"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "_provider_items": [
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "cipher"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "screen",
                    "arguments": "{}",
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ]

    reply(client(handler), messages=messages)

    assert seen["body"]["input"] == [
        {"role": "user", "content": "decide"},
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "cipher"},
        {"type": "function_call", "call_id": "call_1", "name": "screen", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
    ]


def test_reasoning_none_keeps_sampling_parameters() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "completed", "output": message_output()})

    reply(client(handler, reasoning_effort="none"))

    # Effort none is sent explicitly: omitting the field would ask the provider
    # for its default effort, which is not what the operator chose.
    assert seen["body"]["reasoning"] == {"effort": "none"}
    assert seen["body"]["temperature"] == 0.2


def test_a_missing_key_is_reported_rather_than_attempted() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    found = ResponsesClient(
        base_url="https://api.example.test/v1",
        api_key="",
        model="gpt-6-luna",
        transport=httpx.MockTransport(handler),
    )

    result = reply(found)

    assert result.ok is False
    assert "key" in (result.error or "").lower()
    assert called is False


def test_a_rate_limit_and_a_refusal_are_values_not_exceptions() -> None:
    limited = reply(client(lambda request: httpx.Response(429, json={"error": "slow"})))
    refused = reply(
        client(
            lambda request: httpx.Response(
                400, json={"error": {"message": "Model Not Exist"}}
            )
        )
    )

    assert limited.ok is False
    assert "rate limit" in (limited.error or "").lower()
    assert refused.ok is False
    assert "Model Not Exist" in (refused.error or "")


def test_an_empty_or_incomplete_answer_is_an_error() -> None:
    empty = reply(
        client(
            lambda request: httpx.Response(
                200, json={"status": "completed", "output": []}
            )
        )
    )
    incomplete = reply(
        client(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                },
            )
        )
    )

    assert empty.ok is False
    assert "neither" in (empty.error or "").lower()
    assert incomplete.ok is False
    assert "max_output_tokens" in (incomplete.error or "")


def test_a_transport_failure_is_also_a_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = reply(client(handler))

    assert result.ok is False
    assert "ConnectError" in (result.error or "")

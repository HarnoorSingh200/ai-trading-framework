"""Talking to the model provider, where every failure is a value."""

from __future__ import annotations

import json

import httpx
import pytest

from module2.llm import ChatCompletionsClient

pytestmark = pytest.mark.unit

KEY = "sk-test-not-a-real-key"


def client(handler, **overrides) -> ChatCompletionsClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    return ChatCompletionsClient(
        base_url="https://api.example.test",
        api_key=KEY,
        model="deepseek-v4-flash",
        client=httpx.Client(transport=transport, base_url="https://api.example.test"),
        **overrides,
    )


def reply(call: ChatCompletionsClient, **kwargs):  # type: ignore[no-untyped-def]
    return call.complete(
        messages=[{"role": "user", "content": "decide"}],
        tools=[],
        temperature=0.2,
        max_tokens=100,
        timeout=10.0,
        **kwargs,
    )


def test_a_tool_call_comes_back_with_its_arguments_as_an_object() -> None:
    """The provider sends arguments as a JSON *string*; nothing else may parse them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "screen",
                                        "arguments": json.dumps(
                                            {"where": [{"field": "rsi_14", "op": "<", "value": 30}]}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    result = reply(client(handler))

    assert result.ok is True
    assert result.model == "deepseek-flash"
    assert result.tool_calls[0].name == "screen"
    assert result.tool_calls[0].arguments["where"][0]["field"] == "rsi_14"
    assert result.text is None


def test_the_key_goes_in_the_header_and_nowhere_else() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "no trade"}}
                ],
            },
        )

    result = reply(client(handler))

    assert seen["auth"] == f"Bearer {KEY}"
    assert KEY not in seen["body"]
    assert KEY not in json.dumps(result.text or "")


def test_an_openai_v1_base_url_keeps_the_v1_path() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "model": "gpt-6-luna",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "no trade"}}
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    found = ChatCompletionsClient(
        base_url="https://api.openai.com/v1",
        api_key=KEY,
        model="gpt-6-luna",
        client=httpx.Client(
            transport=transport,
            base_url="https://api.openai.com/v1",
        ),
        credential_name="OPENAI_API_KEY",
        provider_name="openai",
    )

    result = reply(found)

    assert result.ok is True
    assert seen["path"] == "/v1/chat/completions"


def test_a_rate_limit_is_a_status_not_an_exception() -> None:
    result = reply(client(lambda request: httpx.Response(429, json={"error": "slow down"})))

    assert result.ok is False
    assert "rate limit" in (result.error or "").lower()


def test_the_providers_own_words_survive_a_four_hundred() -> None:
    """Otherwise a wrong model name is reported as a bare 'HTTP 400'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "Model Not Exist", "type": "invalid_request"}}
        )

    result = reply(client(handler))

    assert result.ok is False
    assert "Model Not Exist" in (result.error or "")


def test_an_empty_answer_is_an_error_rather_than_a_silent_no_trade() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
        )

    result = reply(client(handler))

    assert result.ok is False
    assert "neither" in (result.error or "").lower()


def test_a_reasoning_only_answer_is_kept() -> None:
    """A model that spent the round thinking said something worth recording."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "",
                            "reasoning_content": "Nothing here meets the bar.",
                        },
                    }
                ]
            },
        )

    result = reply(client(handler))

    assert result.ok is True
    assert "Nothing here meets the bar." in (result.text or "")


def test_a_transport_failure_is_also_a_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = reply(client(handler))

    assert result.ok is False
    assert "ConnectError" in (result.error or "")


def test_no_key_is_reported_rather_than_attempted() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    result = reply(
        ChatCompletionsClient(
            base_url="https://api.example.test",
            api_key="",
            model="deepseek-v4-flash",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )

    assert result.ok is False
    assert "key" in (result.error or "").lower()
    assert called is False


def test_provider_only_conversation_keys_never_reach_the_wire() -> None:
    """The Responses client replays raw items; Chat Completions must not see them."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "ok"}}
                ]
            },
        )

    found = client(handler)
    result = found.complete(
        messages=[
            {"role": "user", "content": "decide"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [],
                "_provider_items": [{"type": "reasoning", "id": "rs_1"}],
            },
        ],
        tools=[],
        temperature=0.2,
        max_tokens=100,
        timeout=10.0,
    )

    assert result.ok is True
    assert all("_provider_items" not in message for message in seen["body"]["messages"])

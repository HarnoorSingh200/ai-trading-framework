"""Model-name routing without ever exposing either credential."""

from __future__ import annotations

import pytest

from module2.llm import ChatCompletionsClient
from module2.providers import (
    build_model_client,
    resolve_api_mode,
    resolve_model_route,
    uses_openai,
)
from module2.responses import ResponsesClient
from module2.secrets import LLM_API_KEY, OPENAI_API_KEY

pytestmark = pytest.mark.unit


def route(model: str, *, openai_key: str = "openai-test"):
    return resolve_model_route(
        model,
        default_base_url="https://api.deepseek.com",
        openai_base_url="https://api.openai.com/v1",
        default_key="deepseek-test",
        openai_key=openai_key,
    )


def test_gpt_models_use_the_openai_route_when_its_key_exists() -> None:
    found = route("gpt-6-luna")

    assert uses_openai("gpt-6-luna") is True
    assert found.provider == "openai"
    assert found.base_url == "https://api.openai.com/v1"
    assert found.credential_name == OPENAI_API_KEY
    assert found.api_key == "openai-test"


def test_other_models_keep_the_default_provider() -> None:
    found = route("deepseek-v4-flash")

    assert found.provider == "default"
    assert found.base_url == "https://api.deepseek.com"
    assert found.credential_name == LLM_API_KEY
    assert found.api_key == "deepseek-test"


def test_a_gpt_model_falls_back_to_a_compatible_gateway_without_an_openai_key() -> None:
    """One key can still serve GPT names through the default gateway."""

    found = route("gpt-6-luna", openai_key="")

    assert found.provider == "default"
    assert found.base_url == "https://api.deepseek.com"
    assert found.credential_name == LLM_API_KEY


def test_auto_sends_a_reasoning_gpt_model_to_the_responses_api() -> None:
    """From GPT-5.4 on, reasoning and tool calls cannot share Chat Completions."""

    assert resolve_api_mode("auto", "gpt-6-luna", "medium") == "responses"
    assert resolve_api_mode("auto", "gpt-6-luna", "high") == "responses"
    assert resolve_api_mode("auto", "gpt-6-luna", "none") == "chat"
    assert resolve_api_mode("auto", "deepseek-v4-flash", "medium") == "chat"


def test_an_explicit_api_mode_wins_over_the_rule() -> None:
    assert resolve_api_mode("chat", "gpt-6-luna", "high") == "chat"
    assert resolve_api_mode("responses", "deepseek-v4-flash", "medium") == "responses"


def test_the_factory_builds_the_client_the_mode_calls_for() -> None:
    found = route("gpt-6-luna")
    reasoning = build_model_client(
        route=found, model="gpt-6-luna", api_mode="auto", reasoning_effort="medium"
    )
    plain = build_model_client(
        route=route("deepseek-v4-flash"),
        model="deepseek-v4-flash",
        api_mode="auto",
        reasoning_effort="medium",
    )

    assert isinstance(reasoning, ResponsesClient)
    assert isinstance(plain, ChatCompletionsClient)
    assert plain.has_credential is True

"""Choose the model provider for one agent.

The chat client is OpenAI-compatible, but credentials are not universal: a
DeepSeek key is not an OpenAI key. An agent may therefore carry a model name
such as ``gpt-6-luna`` while another keeps the default model, and each child
process resolves its own endpoint and credential without sharing a secret.

When an OpenAI key is present, ``gpt-*`` models route to the configured OpenAI
base URL. Otherwise they fall back to the default OpenAI-compatible endpoint,
which lets an operator use a gateway that serves those names with one key.
"""

from __future__ import annotations

from dataclasses import dataclass

from module2.llm import AsyncChatClient, ChatClient, ChatCompletionsClient
from module2.responses import DEFAULT_TIMEOUT_SECONDS, ResponsesClient
from module2.secrets import LLM_API_KEY, OPENAI_API_KEY


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """The endpoint and credential one agent will use."""

    provider: str
    base_url: str
    credential_name: str
    api_key: str


def uses_openai(model: str) -> bool:
    """A deliberately narrow convention: GPT model names use the OpenAI key."""

    return model.strip().lower().startswith("gpt-")


def resolve_api_mode(
    configured: str, model: str, reasoning_effort: str | None
) -> str:
    """Which API one model is reached through.

    An explicit chat or responses wins: a gateway that implements only one of
    them knows which it is. Auto is a rule, not a guess: from GPT-5.4 on, a
    provider allows function calling on Chat Completions only when reasoning is
    switched off, so a GPT model asked to think is spoken to on the Responses
    API. Every other model - DeepSeek included - keeps the Chat Completions
    route that the framework was built on.
    """

    value = (configured or "").strip().lower()
    if value in {"chat", "responses"}:
        return value
    effort = (reasoning_effort or "none").strip().lower()
    if effort != "none" and model.strip().lower().startswith("gpt-"):
        return "responses"
    return "chat"


def build_model_client(
    *,
    route: ModelRoute,
    model: str,
    api_mode: str = "auto",
    reasoning_effort: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ChatClient | AsyncChatClient:
    """Build the client for one agent, from its resolved route.

    One place decides this, so the process entry point does not have to know
    which protocol a model name implies.
    """

    mode = resolve_api_mode(api_mode, model, reasoning_effort)
    if mode == "responses":
        return ResponsesClient(
            base_url=route.base_url,
            api_key=route.api_key,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            credential_name=route.credential_name,
            provider_name=route.provider,
        )
    return ChatCompletionsClient(
        base_url=route.base_url,
        api_key=route.api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        credential_name=route.credential_name,
        provider_name=route.provider,
    )


def resolve_model_route(
    model: str,
    *,
    default_base_url: str,
    openai_base_url: str,
    default_key: str,
    openai_key: str,
) -> ModelRoute:
    """Resolve one agent's model without exposing either credential."""

    if uses_openai(model) and openai_key:
        return ModelRoute(
            provider="openai",
            base_url=openai_base_url.rstrip("/"),
            credential_name=OPENAI_API_KEY,
            api_key=openai_key,
        )
    return ModelRoute(
        provider="default",
        base_url=default_base_url.rstrip("/"),
        credential_name=LLM_API_KEY,
        api_key=default_key,
    )

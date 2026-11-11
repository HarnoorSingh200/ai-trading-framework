"""The OpenAI Responses API, behind the same narrow protocol as Chat Completions.

GPT-5.4 and later cannot both reason and call tools on Chat Completions: the
provider allows function calling there only when reasoning is switched off. A
trading agent that is expected to think therefore has to be spoken to on the
Responses API, and this is that transport.

The conversion is deliberately local and one-way. The worker keeps the same
provider-agnostic Reply and message list it always had; this module turns them
into Responses input items and back. Nothing is stored on the provider's side
(store is false). The encrypted reasoning items the API returns are attached to
the assistant turn and replayed with the next call, which is what keeps the
chain of thought across the rounds of one candle without keeping a conversation
on OpenAI's servers.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from module2.llm import (
    STATUS_NO_CREDENTIAL,
    STATUS_PROVIDER_ERROR,
    STATUS_RATE_LIMITED,
    STATUS_TIMEOUT,
    Reply,
    ToolCall,
    provider_error_detail,
)

#: How long one model call may take when the caller does not say.
DEFAULT_TIMEOUT_SECONDS: float = 120.0

#: The output items worth replaying into the next call of a cycle. A message
#: carries the visible answer, a function_call is the tool request, and a
#: reasoning item carries the encrypted thinking the provider wants back.
_REPLAYED_ITEM_TYPES: frozenset[str] = frozenset(
    {"message", "function_call", "reasoning"}
)


class ResponsesClient:
    """One OpenAI Responses model, called once per round.

    Stateless by design: each call sends the whole conversation again, with
    store false, so a decision can be reproduced from this tree rather than
    from provider-side history.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str | None = "medium",
        store: bool = False,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        credential_name: str = "OPENAI_API_KEY",
        provider_name: str = "the model provider",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._store = store
        self._transport = transport
        self._credential_name = credential_name
        self._provider_name = provider_name
        self._timeout_seconds = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @property
    def has_credential(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        """Nothing to close: a call opens its own short-lived async client.

        A long-lived AsyncClient would be bound to whichever event loop first
        used it, and this worker runs one loop per cycle. The connection cost
        is small next to a reasoning model's own latency.
        """

    async def acomplete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Reply:
        if not self._api_key:
            return self._failure(
                STATUS_NO_CREDENTIAL,
                (
                    f"no {self._provider_name} API key is configured; set "
                    f"{self._credential_name} in the environment or in the file "
                    "named by FRAMEWORK_SECRETS_FILE"
                ),
            )

        body: dict[str, Any] = {
            "model": self._model,
            "input": _responses_input(messages),
            "store": self._store,
        }
        instructions = _responses_instructions(messages)
        if instructions:
            body["instructions"] = instructions
        wire_tools = _responses_tools(tools)
        if wire_tools:
            body["tools"] = wire_tools
        if self._reasoning_effort:
            body["reasoning"] = {"effort": self._reasoning_effort}
        if max_tokens:
            body["max_output_tokens"] = max_tokens
        # Reasoning models do not take sampling parameters. The provider ignores
        # them today, but sending one with reasoning is asking for a silent
        # difference between what this request says and what it means.
        if self._reasoning_effort in (None, "none"):
            body["temperature"] = temperature

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/responses",
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TimeoutException as exc:
            return self._failure(
                STATUS_TIMEOUT, f"the model timed out: {type(exc).__name__}"
            )
        except httpx.HTTPError as exc:
            return self._failure(
                STATUS_PROVIDER_ERROR,
                f"the model is unreachable: {type(exc).__name__}: {exc}",
            )
        latency = (time.perf_counter() - started) * 1000
        if response.status_code == 429:
            return self._failure(
                STATUS_RATE_LIMITED, "the provider rate limit was hit", latency
            )
        if response.status_code >= 400:
            return self._failure(
                STATUS_PROVIDER_ERROR,
                f"HTTP {response.status_code}: {provider_error_detail(response)}",
                latency,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return self._failure(
                STATUS_PROVIDER_ERROR,
                f"the provider's reply could not be read: {exc}",
                latency,
            )
        if not isinstance(payload, Mapping):
            return self._failure(
                STATUS_PROVIDER_ERROR,
                "the provider's reply was not an object",
                latency,
            )
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = error.get("message") or error.get("type") or "the provider refused"
            return self._failure(STATUS_PROVIDER_ERROR, str(message)[:400], latency)

        status = str(payload.get("status") or "")
        output = payload.get("output")
        items = (
            tuple(dict(item) for item in output if isinstance(item, Mapping))
            if isinstance(output, Sequence)
            else ()
        )
        text, calls = _read_output(items)
        if status == "incomplete" and not text and not calls:
            reason = payload.get("incomplete_details")
            detail = (
                reason.get("reason")
                if isinstance(reason, Mapping)
                else "the provider stopped before answering"
            )
            return self._failure(
                STATUS_PROVIDER_ERROR,
                f"the provider stopped before answering: {detail}",
                latency,
            )
        if not text and not calls:
            return self._failure(
                STATUS_PROVIDER_ERROR,
                "the provider returned neither content nor a tool call",
                latency,
            )
        return Reply(
            ok=True,
            model=str(payload.get("model") or self._model),
            text=text,
            tool_calls=tuple(calls),
            usage=_responses_usage(payload.get("usage")),
            latency_ms=latency,
            provider_items=tuple(
                item
                for item in items
                if str(item.get("type") or "") in _REPLAYED_ITEM_TYPES
            ),
        )

    def _failure(self, status: str, error: str, latency_ms: float = 0.0) -> Reply:
        return Reply(
            ok=False,
            model=self._model,
            status=status,
            error=error[:400],
            latency_ms=latency_ms,
        )


def _responses_instructions(messages: Sequence[Mapping[str, Any]]) -> str:
    """The system half, which the Responses API calls instructions."""

    parts = [
        str(message.get("content") or "").strip()
        for message in messages
        if str(message.get("role") or "") == "system"
        and str(message.get("content") or "").strip()
    ]
    return "\n\n".join(parts)


def _responses_input(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The message list as Responses input items.

    An assistant turn that carries raw provider items is replayed as those
    items, whole and in order: the reasoning item must sit exactly where the
    provider put it, immediately before the function calls it produced. Tool
    results become function_call_output items keyed by the same call id.
    """

    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "system":
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant":
            raw = message.get("_provider_items")
            if isinstance(raw, (list, tuple)) and raw:
                items.extend(dict(item) for item in raw if isinstance(item, Mapping))
                continue
            content = str(message.get("content") or "")
            if content:
                items.append({"role": "assistant", "content": content})
            continue
        if role in {"user", "developer"}:
            items.append({"role": role, "content": str(message.get("content") or "")})
    return items


def _responses_tools(
    tools: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Chat Completions tool schemas, flattened into the Responses shape."""

    flat: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        if isinstance(function, Mapping):
            flat.append(
                {
                    "type": "function",
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "parameters": dict(function.get("parameters") or {}),
                }
            )
        elif tool.get("type") == "function" and tool.get("name"):
            flat.append(dict(tool))
    return flat


def _read_output(
    items: tuple[dict[str, Any], ...],
) -> tuple[str | None, list[ToolCall]]:
    """Visible text and tool calls, in the shape the worker already reads."""

    texts: list[str] = []
    calls: list[ToolCall] = []
    for item in items:
        kind = str(item.get("type") or "")
        if kind == "message":
            content = item.get("content")
            if isinstance(content, Sequence):
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    if part.get("type") == "output_text" and part.get("text"):
                        texts.append(str(part["text"]))
        elif kind == "function_call":
            raw_arguments = item.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
                except json.JSONDecodeError:
                    arguments = {}
            elif isinstance(raw_arguments, Mapping):
                arguments = dict(raw_arguments)
            else:
                arguments = {}
            calls.append(
                ToolCall(
                    call_id=str(item.get("call_id") or item.get("id") or f"call_{len(calls)}"),
                    name=str(item.get("name") or ""),
                    arguments=arguments,
                )
            )
    text = "\n".join(texts) if texts else None
    return text, calls


def _responses_usage(raw: Any) -> dict[str, Any]:
    """The provider's tokens in the flat vocabulary the activity log uses.

    input_tokens and output_tokens are the two totals; cached_tokens inside the
    input details is what a prompt cache saved, and reasoning_tokens inside the
    output details is how much of the answer was thinking.
    """

    if not isinstance(raw, Mapping):
        return {}
    usage: dict[str, Any] = {}
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    if isinstance(input_tokens, int):
        usage["prompt_tokens"] = input_tokens
    if isinstance(output_tokens, int):
        usage["completion_tokens"] = output_tokens
    input_details = raw.get("input_tokens_details")
    if isinstance(input_details, Mapping):
        cached = input_details.get("cached_tokens")
        if isinstance(cached, int):
            usage["prompt_cache_hit_tokens"] = cached
            if isinstance(input_tokens, int):
                usage["prompt_cache_miss_tokens"] = max(0, input_tokens - cached)
    output_details = raw.get("output_tokens_details")
    if isinstance(output_details, Mapping):
        reasoning = output_details.get("reasoning_tokens")
        if isinstance(reasoning, int):
            usage["reasoning_tokens"] = reasoning
    return usage

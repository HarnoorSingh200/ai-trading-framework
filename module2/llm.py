"""The model provider, behind one narrow protocol.

One call in, one reply out, and **every failure is a value rather than an
exception**: a timeout, a rate limit or a provider that answers with neither
text nor a tool call produces a :class:`Reply` with ``ok=False`` and a reason.
A cycle that cannot reach its model must record that and move on, never take
the process down with it.

The wire format is the OpenAI-compatible chat completions one, which is what the
provider speaks: tool arguments arrive as a JSON *string* inside ``tool_calls``,
and the response may carry reasoning tokens instead of content. Both are handled
here so nothing above this module has to know.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

import httpx

from module2.timeutil import utc_now

#: Statuses a caller may want to treat differently.
STATUS_OK: Final[str] = "ok"
STATUS_RATE_LIMITED: Final[str] = "rate_limited"
STATUS_TIMEOUT: Final[str] = "timeout"
STATUS_PROVIDER_ERROR: Final[str] = "provider_error"
STATUS_REFUSED: Final[str] = "refused"
STATUS_NO_CREDENTIAL: Final[str] = "no_credential"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool the model asked for."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True, slots=True)
class Reply:
    """What the provider returned, including how it failed."""

    ok: bool
    model: str
    status: str = STATUS_OK
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    #: The provider's own output items, when it has a shape worth replaying.
    #: The Responses API returns encrypted reasoning with each tool call; a
    #: later call in the same cycle has to be given those items back or the
    #: chain of thought is lost between rounds. Chat Completions has none.
    provider_items: tuple[dict[str, Any], ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class ChatClient(Protocol):
    """The only thing the agent loop knows about a model provider."""

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Reply: ...


class ChatCompletionsClient:
    """An OpenAI-compatible ``/chat/completions`` client."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 120.0,
        credential_name: str = "DEEPSEEK_API_KEY",
        provider_name: str = "the model provider",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._credential_name = credential_name
        self._provider_name = provider_name
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout_seconds
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def has_credential(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> Reply:
        if not self._api_key:
            return Reply(
                ok=False,
                model=self._model,
                status=STATUS_NO_CREDENTIAL,
                error=(
                    f"no {self._provider_name} API key is configured; set "
                    f"{self._credential_name} in the environment or in the file "
                    "named by FRAMEWORK_SECRETS_FILE"
                ),
            )
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [_wire_message(message) for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = [dict(tool) for tool in tools]
            # A model that may call tools must be free to answer in prose or in
            # tool calls, so the JSON response format is deliberately not asked
            # for: the providers that support both do not support them together.
        started = time.perf_counter()
        try:
            response = self._client.post(
                "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            return self._failure(STATUS_TIMEOUT, f"the model timed out: {type(exc).__name__}")
        except httpx.HTTPError as exc:
            return self._failure(
                STATUS_PROVIDER_ERROR, f"the model is unreachable: {type(exc).__name__}: {exc}"
            )
        latency = (time.perf_counter() - started) * 1000
        if response.status_code == 429:
            return self._failure(STATUS_RATE_LIMITED, "the provider rate limit was hit", latency)
        if response.status_code >= 400:
            return self._failure(
                STATUS_PROVIDER_ERROR,
                f"HTTP {response.status_code}: {provider_error_detail(response)}",
                latency,
            )
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return self._failure(
                STATUS_PROVIDER_ERROR, f"the provider's reply could not be read: {exc}", latency
            )
        finish = str(choice.get("finish_reason") or "")
        if finish == "content_filter":
            return self._failure(
                STATUS_REFUSED, "the provider refused the request", latency
            )
        raw_content = message.get("content")
        # A provider that returns tool calls sends an empty content string
        # beside them. Blank is absence here, not an empty answer.
        text = None if raw_content is None or not str(raw_content).strip() else str(raw_content)
        if not text and message.get("reasoning_content"):
            # A reasoning model can spend the whole round thinking and return an
            # empty answer. Keeping the trace is the difference between "it said
            # nothing" and knowing what it was working through.
            text = f"[reasoning only] {message['reasoning_content']}"[:4000]
        calls = _tool_calls(message.get("tool_calls"))
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
            tool_calls=calls,
            usage=_normalised_usage(payload.get("usage")),
            latency_ms=latency,
        )

    async def acomplete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> Reply:
        """The same one call, awaited.

        The transport underneath is synchronous httpx, so the call is run in a
        worker thread rather than rewritten: every leaf request in this tree is
        synchronous, and a thread is the honest adapter between the two styles.
        """

        return await asyncio.to_thread(
            self.complete,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def _failure(self, status: str, error: str, latency_ms: float = 0.0) -> Reply:
        return Reply(
            ok=False, model=self._model, status=status, error=error[:400], latency_ms=latency_ms
        )


@runtime_checkable
class AsyncChatClient(Protocol):
    """The same one call, awaited. The worker always speaks this shape."""

    async def acomplete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> Reply: ...


class _ThreadedChatClient:
    """A synchronous client behind the async protocol.

    Every leaf request in this tree is synchronous httpx, so a thread is the
    honest adapter between the two styles. It also means a test client written
    against the old protocol keeps working unchanged.
    """

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    async def acomplete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> Reply:
        return await asyncio.to_thread(
            self._client.complete,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )


def as_async_client(client: ChatClient | AsyncChatClient) -> AsyncChatClient:
    """Return something the worker can await, whatever it was handed."""

    acomplete = getattr(client, "acomplete", None)
    if callable(acomplete):
        return client  # type: ignore[return-value]
    return _ThreadedChatClient(client)


class ScriptedClient:
    """A canned provider, for tests and for a dry run with no key.

    Each item may be a :class:`Reply`, a string (the model's prose), a list of
    ``(name, arguments)`` pairs to be returned as tool calls, or an exception
    (to prove the loop survives a client that misbehaves).
    """

    def __init__(self, script: Iterable[Any] | None = None) -> None:
        self._script: list[Any] = list(script or [])
        self.requests: list[dict[str, Any]] = []

    def queue(self, *items: Any) -> None:
        self._script.extend(items)

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> Reply:
        self.requests.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": [dict(tool) for tool in tools],
            }
        )
        # An exhausted script says nothing rather than deciding: a test client
        # that quietly produced decisions would make "did it decide?" untestable.
        item = self._script.pop(0) if self._script else "Nothing was scripted for this cycle."
        if isinstance(item, Reply):
            return item
        if isinstance(item, Exception):
            return Reply(
                ok=False,
                model="scripted",
                status=STATUS_PROVIDER_ERROR,
                error=f"{type(item).__name__}: {item}"[:400],
            )
        if isinstance(item, str):
            return Reply(ok=True, model="scripted", text=item)
        if isinstance(item, Mapping):
            return _scripted_call(item)
        raise TypeError(f"a scripted item must be a Reply, str, mapping or Exception: {item!r}")


def _scripted_call(item: Mapping[str, Any]) -> Reply:
    """A mapping is either a decision or a ``{tool, arguments}`` call."""

    if "tool" in item:
        return Reply(
            ok=True,
            model="scripted",
            tool_calls=(
                ToolCall(
                    call_id=f"call_{abs(hash(str(item))) % 10_000}",
                    name=str(item["tool"]),
                    arguments=dict(item.get("arguments") or {}),
                ),
            ),
        )
    return Reply(
        ok=True,
        model="scripted",
        tool_calls=(ToolCall(call_id="call_decision", name="submit_decision", arguments=dict(item)),),
    )


def provider_error_detail(response: httpx.Response) -> str:
    """The provider's own message, which is the only thing that explains a 4xx."""

    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type")
            if message:
                return str(message)[:200]
        if error:
            return str(error)[:200]
        if body.get("message"):
            return str(body["message"])[:200]
    return (response.text or "")[:200]


def _tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    """Read the provider's tool calls, tolerating a malformed argument blob."""

    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, Mapping):
            arguments = {}
        calls.append(
            ToolCall(
                call_id=str(item.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=dict(arguments),
            )
        )
    return tuple(calls)


def _wire_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """A message as the wire takes it.

    Keys that begin with an underscore are conversation bookkeeping for another
    provider, never part of the request: the Responses client replays the raw
    output items it finds there, and Chat Completions must not be sent them.
    """

    return {
        str(key): value
        for key, value in message.items()
        if not str(key).startswith("_")
    }


def _normalised_usage(raw: Any) -> dict[str, Any]:
    """The provider's usage block with the reasoning count where a reader looks.

    Chat Completions nests it under completion_tokens_details; the worker logs
    a flat reasoning_tokens next to the two totals so one event answers "how
    much of that was thinking".
    """

    usage = dict(raw) if isinstance(raw, Mapping) else {}
    details = usage.get("completion_tokens_details")
    if isinstance(details, Mapping) and usage.get("reasoning_tokens") is None:
        reasoning = details.get("reasoning_tokens")
        if isinstance(reasoning, int):
            usage["reasoning_tokens"] = reasoning
    return usage


def now_stamp() -> str:
    """A timestamp for a log line, in the same shape as everything else."""

    return utc_now().isoformat()

"""Minimal async OpenRouter client with tool calling and reasoning continuity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from .models import AssistantTurn, ProviderMessage, ToolCall, coerce_provider_message


class CompletionClient(Protocol):
    """The narrow interface consumed by ``AgentRuntime`` and test fakes."""

    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> AssistantTurn: ...


class OpenRouterError(RuntimeError):
    """A provider/API failure with no request secret included in its text."""


SleepFunction = Callable[[float], Awaitable[None]]


class OpenRouterClient:
    """OpenRouter chat-completions client using the documented raw HTTP schema.

    ``provider`` and ``reasoning`` are top-level OpenRouter fields.  If this
    client is later replaced with an OpenAI SDK, the same mapping can be passed
    as that SDK's ``extra_body``.  Keeping it here makes the request auditable
    and straightforward to mock in tests.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        provider: str | None = "baseten",
        providers: Sequence[str] | None = None,
        allow_fallbacks: bool = False,
        require_parameters: bool = True,
        parallel_tool_calls: bool = False,
        reasoning_enabled: bool = True,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        max_retry_delay_seconds: float = 8.0,
        extra_body: Mapping[str, Any] | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: SleepFunction = asyncio.sleep,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenRouter API key is required")
        if not model.strip():
            raise ValueError("OpenRouter model is required")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff_seconds < 0 or max_retry_delay_seconds < 0:
            raise ValueError("retry delays must not be negative")
        self.model = model
        self.base_url = base_url.rstrip("/")
        if provider is not None and providers is not None:
            raise ValueError("Pass either provider or providers, not both")
        raw_providers: Sequence[str] = providers if providers is not None else ((provider,) if provider else ())
        normalised_providers = tuple(
            candidate.strip().lower()
            for candidate in raw_providers
            if isinstance(candidate, str) and candidate.strip()
        )
        if len(set(normalised_providers)) != len(normalised_providers):
            raise ValueError("OpenRouter provider order must not contain duplicates")
        self.providers = normalised_providers
        # Kept as a compatibility alias for code that needs the preferred
        # endpoint; payload construction always uses the full order.
        self.provider = self.providers[0] if self.providers else None
        self.allow_fallbacks = allow_fallbacks
        self.require_parameters = require_parameters
        self.parallel_tool_calls = parallel_tool_calls
        self.reasoning_enabled = reasoning_enabled
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.extra_body = dict(extra_body or {})
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Helpful during development: OpenRouter can report which endpoint
            # actually served the call without exposing model reasoning.
            "X-OpenRouter-Metadata": "enabled",
        }
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = http_client is None
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Any) -> "OpenRouterClient":
        """Build from ``Settings`` without importing config at module import time."""

        return cls(
            api_key=settings.require_openrouter_api_key(),
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            provider=None,
            providers=settings.openrouter_provider_order,
            allow_fallbacks=settings.openrouter_allow_fallbacks,
            require_parameters=settings.openrouter_require_parameters,
            parallel_tool_calls=settings.openrouter_parallel_tool_calls,
            reasoning_enabled=settings.openrouter_reasoning_enabled,
            timeout_seconds=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
            retry_backoff_seconds=settings.openrouter_retry_backoff_seconds,
            max_retry_delay_seconds=settings.openrouter_max_retry_delay_seconds,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def build_payload(
        self,
        *,
        messages: Sequence[ProviderMessage | dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> dict[str, Any]:
        """Build an inspectable provider payload without sending it."""

        typed_messages = [coerce_provider_message(message) for message in messages]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.to_provider_dict() for message in typed_messages],
            "tools": tools,
            "tool_choice": tool_choice,
        }
        # The Python runtime executes returned calls one by one regardless of
        # this optional OpenAI-compatible hint.  Do not send the ``false``
        # value: with ``require_parameters=true`` it would exclude a valid
        # provider endpoint that does not advertise this optional parameter.
        # Send it only when a future caller explicitly opts into parallel
        # provider calls.
        if self.parallel_tool_calls:
            payload["parallel_tool_calls"] = True
        # Extra body is deliberately supported for future provider-specific
        # fields, like OpenAI SDK's extra_body.  Required controls below remain
        # explicit so configuration cannot accidentally disable them.
        payload.update(self.extra_body)
        payload["reasoning"] = {"enabled": self.reasoning_enabled}
        if self.providers:
            payload["provider"] = {
                "order": list(self.providers),
                # ``order`` is only a preference.  With fallbacks enabled,
                # OpenRouter may otherwise choose an arbitrary endpoint after
                # the preferred ones fail.  ``only`` makes the fallback set a
                # real allow-list while retaining availability within it.
                "only": list(self.providers),
                "allow_fallbacks": self.allow_fallbacks,
                "require_parameters": self.require_parameters,
            }
        return payload

    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> AssistantTurn:
        payload = self.build_payload(messages=messages, tools=tools, tool_choice=tool_choice)
        try:
            response = await self._post_with_retries(payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # OpenRouter's body may be useful for operators, but it can be
            # enormous and must not end up in an end-user response/trace.
            raise OpenRouterError(f"OpenRouter returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter request failed: {type(exc).__name__}") from exc

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise OpenRouterError("OpenRouter returned invalid JSON") from exc
        return self.extract_assistant_turn(body)

    async def _post_with_retries(self, payload: dict[str, Any]) -> httpx.Response:
        """Retry only transient provider responses before any tool can run.

        A chat-completions request is safe to resend at this point: a returned
        model tool call has not been executed yet.  We deliberately do not add
        retry logic in ``AgentRuntime`` after a tool has produced a side effect.
        """

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=payload,
                )
            except httpx.TransportError:
                # A read/connect/protocol failure occurred before there was a
                # model response.  No tool call could have reached the Python
                # runtime yet, so retrying this completion is safe.
                if attempt == self.max_retries:
                    raise
                await self._sleep(self._backoff_delay_seconds(attempt))
                continue
            if not _is_retryable_status(response.status_code) or attempt == self.max_retries:
                return response
            await self._sleep(self._retry_delay_seconds(response, attempt))

        # The loop always returns; this exists only to make static analyzers
        # understand that response cannot be None.
        assert response is not None
        return response

    def _retry_delay_seconds(self, response: httpx.Response, attempt: int) -> float:
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        if retry_after is None:
            retry_after = self._backoff_delay_seconds(attempt)
        return min(max(retry_after, 0.0), self.max_retry_delay_seconds)

    def _backoff_delay_seconds(self, attempt: int) -> float:
        return min(
            self.retry_backoff_seconds * (2**attempt),
            self.max_retry_delay_seconds,
        )

    @staticmethod
    def extract_assistant_turn(body: Mapping[str, Any]) -> AssistantTurn:
        """Normalise a raw OpenRouter/OpenAI-compatible completion response."""

        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError("OpenRouter response did not contain choices[0].message") from exc
        if not isinstance(message, Mapping):
            raise OpenRouterError("OpenRouter response message was not an object")

        content = message.get("content")
        if content is None:
            content_text = ""
        elif isinstance(content, str):
            content_text = content
        else:
            # Some compatible providers return content blocks.  Preserve their
            # visible content safely rather than crashing the agent loop.
            content_text = json.dumps(content, ensure_ascii=False, default=str)

        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise OpenRouterError("OpenRouter response tool_calls was not a list")
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                raise OpenRouterError(f"OpenRouter tool call {index} was not an object")
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                raise OpenRouterError(f"OpenRouter tool call {index} had no function")
            name = function.get("name")
            call_id = raw_call.get("id") or f"call_{index}"
            raw_arguments = function.get("arguments", "{}")
            arguments, parse_error, arguments_text = _parse_tool_arguments(raw_arguments)
            tool_calls.append(
                ToolCall(
                    id=str(call_id),
                    name=str(name or "unknown_tool"),
                    arguments=arguments,
                    raw_arguments=arguments_text,
                    arguments_error=parse_error,
                )
            )

        # Preserve every compatible reasoning field exactly.  They are kept
        # out of trace/model dumps by AssistantTurn's excluded fields.
        reasoning_details_present = "reasoning_details" in message
        reasoning_present = "reasoning" in message

        return AssistantTurn(
            content=content_text,
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason")) if choice.get("finish_reason") else None,
            reasoning_details=message.get("reasoning_details"),
            reasoning=message.get("reasoning"),
            reasoning_details_present=reasoning_details_present,
            reasoning_present=reasoning_present,
        )


def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None, str]:
    """Parse model arguments while preserving malformed raw JSON for continuity."""

    if isinstance(raw_arguments, Mapping):
        return dict(raw_arguments), None, json.dumps(raw_arguments, ensure_ascii=False)
    if raw_arguments is None:
        return {}, "arguments were null", "{}"
    arguments_text = str(raw_arguments)
    try:
        parsed = json.loads(arguments_text)
    except json.JSONDecodeError as exc:
        return {}, str(exc), arguments_text
    if not isinstance(parsed, dict):
        return {}, "arguments must decode to a JSON object", arguments_text
    return parsed, None, arguments_text


def _is_retryable_status(status_code: int) -> bool:
    """OpenRouter recommends retrying rate limits and temporary server errors."""

    return status_code == 429 or 500 <= status_code <= 599


def _retry_after_seconds(value: str | None) -> float | None:
    """Accept both legal ``Retry-After`` forms without throwing on bad headers."""

    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return (retry_at - datetime.now(timezone.utc)).total_seconds()

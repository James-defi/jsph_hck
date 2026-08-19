from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from agent.app.config import Settings
from agent.app.models import ProviderMessage
from agent.app.openrouter import OpenRouterClient


def test_payload_routes_to_baseten_and_keeps_reasoning_enabled() -> None:
    client = OpenRouterClient(
        api_key="test-key",
        model="deepseek/deepseek-v4-flash-0731",
        provider="Baseten",
        allow_fallbacks=False,
        require_parameters=True,
        parallel_tool_calls=False,
    )

    payload = client.build_payload(
        messages=[ProviderMessage(role="user", content="Найди маршрут")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_avia",
                    "description": "Search flights",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert payload["model"] == "deepseek/deepseek-v4-flash-0731"
    assert payload["reasoning"] == {"enabled": True}
    # Calls remain sequential in ``AgentRuntime``.  Omitting this optional
    # compatibility parameter lets require_parameters select Baseten's tool
    # endpoint, which does not advertise ``parallel_tool_calls``.
    assert "parallel_tool_calls" not in payload
    assert payload["provider"] == {
        "order": ["baseten"],
        "only": ["baseten"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_settings_and_client_use_only_vetted_provider_fallbacks() -> None:
    settings = Settings(openrouter_providers="Baseten, DeepInfra, Fireworks, StreamLake")
    client = OpenRouterClient.from_settings(settings)
    try:
        payload = client.build_payload(
            messages=[ProviderMessage(role="user", content="Найди маршрут")],
            tools=[],
        )
    finally:
        # No request is sent in this test, but close the owned async client.
        import asyncio

        asyncio.run(client.aclose())

    assert settings.openrouter_provider_order == (
        "baseten",
        "deepinfra",
        "fireworks",
        "streamlake",
    )
    assert payload["provider"] == {
        "order": ["baseten", "deepinfra", "fireworks", "streamlake"],
        "only": ["baseten", "deepinfra", "fireworks", "streamlake"],
        "allow_fallbacks": True,
        "require_parameters": True,
    }
    with pytest.raises(ValidationError):
        Settings(openrouter_providers="baseten,not-a-vetted-provider")


def test_reasoning_fields_are_preserved_for_the_next_provider_turn_only() -> None:
    private_details = [{"type": "reasoning.text", "text": "private chain"}]
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "Ищу варианты",
                    "reasoning": "private fallback reasoning",
                    "reasoning_details": private_details,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_avia",
                                "arguments": '{"origin":"MOW","destination":"IST"}',
                            },
                        }
                    ],
                },
            }
        ]
    }

    turn = OpenRouterClient.extract_assistant_turn(body)
    provider_message = turn.to_provider_message().to_provider_dict()

    assert provider_message["reasoning"] == "private fallback reasoning"
    assert provider_message["reasoning_details"] == private_details
    assert provider_message["tool_calls"][0]["function"]["arguments"] == (
        '{"origin":"MOW","destination":"IST"}'
    )
    # Ordinary model serialisation is what feeds trace/UI models. It must not
    # leak hidden provider reasoning.
    dumped = turn.model_dump()
    assert "reasoning" not in dumped
    assert "reasoning_details" not in dumped


@pytest.mark.asyncio
async def test_retries_429_before_model_tool_calls_are_executed() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Подбор завершён"},
                    }
                ]
            },
            request=request,
        )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(
        api_key="test-key",
        model="deepseek/deepseek-v4-flash-0731",
        http_client=http_client,
        max_retries=1,
        retry_backoff_seconds=0.0,
        sleep=fake_sleep,
    )
    try:
        turn = await client.complete(
            messages=[ProviderMessage(role="user", content="Найди маршрут")],
            tools=[],
        )
    finally:
        await http_client.aclose()

    assert turn.content == "Подбор завершён"
    assert attempts == 2
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_retries_transient_read_timeout_before_tool_execution() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("upstream did not answer", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "Подбор завершён"},
                    }
                ]
            },
            request=request,
        )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenRouterClient(
        api_key="test-key",
        model="deepseek/deepseek-v4-flash-0731",
        http_client=http_client,
        max_retries=1,
        retry_backoff_seconds=0.125,
        sleep=fake_sleep,
    )
    try:
        turn = await client.complete(
            messages=[ProviderMessage(role="user", content="Найди маршрут")],
            tools=[],
        )
    finally:
        await http_client.aclose()

    assert turn.content == "Подбор завершён"
    assert attempts == 2
    assert sleeps == [0.125]

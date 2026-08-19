from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from agent.app.models import AssistantTurn, ProviderMessage, RunStatus, ToolCall
from agent.app.runtime import AgentRuntime
from agent.app.tool_registry import ToolDefinition, ToolRegistry


class ScriptedLLM:
    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns
        self.calls: list[tuple[list[ProviderMessage], list[dict[str, Any]]]] = []

    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> AssistantTurn:
        self.calls.append((list(messages), tools))
        return self.turns.pop(0)


@pytest.mark.asyncio
async def test_runtime_executes_registered_tool_and_keeps_reasoning_out_of_trace() -> None:
    seen_arguments: list[dict[str, Any]] = []

    async def search_avia(arguments: dict[str, Any]) -> dict[str, Any]:
        seen_arguments.append(arguments)
        return {"offers": [{"id": "offer-1"}]}

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="search_avia",
                description="Find aviation offers",
                parameters={"type": "object", "properties": {"origin": {"type": "string"}}},
                handler=search_avia,
            )
        ]
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call-search", name="search_avia", arguments={"origin": "MOW"})],
                reasoning_details=[{"text": "do not trace this"}],
                reasoning_details_present=True,
            ),
            AssistantTurn(content="## Баланс\nПодходящий вариант найден.", finish_reason="stop"),
        ]
    )

    result = await AgentRuntime(llm=llm, tools=registry, max_steps=4).run("Найди маршрут")

    assert result.status is RunStatus.COMPLETED
    assert result.answer.startswith("## Баланс")
    assert seen_arguments == [{"origin": "MOW"}]
    assert len(llm.calls) == 2
    # The exact private field is still available to OpenRouter on continuation.
    second_request = [item.to_provider_dict() for item in llm.calls[1][0]]
    assert second_request[-2]["reasoning_details"] == [{"text": "do not trace this"}]
    assert second_request[-1]["role"] == "tool"
    assert second_request[-1]["tool_call_id"] == "call-search"
    # Tools are resent on every model request as required by the tool-calling
    # protocol, while the UI trace has no hidden chain/reasoning data.
    assert llm.calls[0][1] == llm.calls[1][1]
    trace_json = json.dumps(result.trace.model_dump(mode="json"), ensure_ascii=False)
    assert "do not trace this" not in trace_json


@pytest.mark.asyncio
async def test_successful_terminal_tool_ends_loop_without_another_model_request() -> None:
    async def plan_group_sync(_: dict[str, Any]) -> dict[str, Any]:
        return {"summary": "Найдено 2 проверяемых сценария."}

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="plan_group_sync",
                description="Build the complete group plan",
                parameters={"type": "object", "properties": {}},
                handler=plan_group_sync,
                finishes_agent_run=True,
            )
        ]
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="plan-1", name="plan_group_sync", arguments={})],
            )
        ]
    )

    result = await AgentRuntime(llm=llm, tools=registry, max_steps=4).run("Собери группу")

    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Найдено 2 проверяемых сценария."
    assert result.steps == 1
    assert len(llm.calls) == 1
    assert [event.kind for event in result.trace.events] == ["model", "tool", "runtime"]


@pytest.mark.asyncio
async def test_explicit_selection_tool_is_not_available_in_initial_run() -> None:
    invoked = False

    async def checkout(_: dict[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return {"checkout_url": "https://example.test/checkout"}

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="create_checkout_link",
                description="Create the handoff after a selected fare",
                parameters={"type": "object", "properties": {"checkout_ref": {"type": "string"}}},
                handler=checkout,
                requires_explicit_user_selection=True,
            )
        ]
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call-checkout",
                        name="create_checkout_link",
                        arguments={"checkout_ref": "not-selected"},
                    )
                ]
            ),
            AssistantTurn(content="Нужен выбор тарифа.", finish_reason="stop"),
        ]
    )

    result = await AgentRuntime(llm=llm, tools=registry, max_steps=3).run("Дай ссылку")

    assert result.status is RunStatus.COMPLETED
    assert invoked is False
    assert registry.schemas() == []
    tool_event = next(event for event in result.trace.events if event.kind == "tool")
    assert tool_event.tool_execution is not None
    assert tool_event.tool_execution.success is False
    assert "explicit user selection" in (tool_event.tool_execution.error or "")


@pytest.mark.asyncio
async def test_explicit_selection_tool_can_be_enabled_for_selected_tariff_flow() -> None:
    async def checkout(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"checkout_ref": arguments["checkout_ref"], "checkout_url": "https://example.test/checkout"}

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="create_checkout_link",
                description="Create selected checkout handoff",
                parameters={"type": "object", "properties": {"checkout_ref": {"type": "string"}}},
                handler=checkout,
                requires_explicit_user_selection=True,
            )
        ]
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call-selected-checkout",
                        name="create_checkout_link",
                        arguments={"checkout_ref": "selected-fare-ref"},
                    )
                ]
            ),
            AssistantTurn(content="Ссылка готова.", finish_reason="stop"),
        ]
    )

    result = await AgentRuntime(llm=llm, tools=registry, max_steps=3).run(
        "Оформи выбранный тариф",
        allowed_tool_names={"create_checkout_link"},
    )

    assert result.status is RunStatus.COMPLETED
    tool_event = next(event for event in result.trace.events if event.kind == "tool")
    assert tool_event.tool_execution is not None
    assert tool_event.tool_execution.success is True
    assert tool_event.tool_execution.result["checkout_ref"] == "selected-fare-ref"

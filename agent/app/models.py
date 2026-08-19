"""Typed transport models for the agent loop.

``reasoning`` and ``reasoning_details`` are deliberately excluded from ordinary
model dumps so a trace cannot accidentally persist hidden model reasoning. They
are retained only long enough to be sent back to the provider on the next model
turn, as required by reasoning-capable OpenRouter models.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(StrEnum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    FAILED = "failed"


class ToolCall(BaseModel):
    """One OpenAI-compatible function call requested by the model."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Keep raw invalid JSON for a protocol-correct assistant message, but do
    # not include it in normal serialisation or agent trace output.
    raw_arguments: str | None = Field(default=None, exclude=True, repr=False)
    arguments_error: str | None = None

    @field_validator("id", "name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("tool call id and name must not be blank")
        return value

    def to_provider_dict(self) -> dict[str, Any]:
        """Return the OpenAI/OpenRouter wire representation of this call."""

        arguments = self.raw_arguments
        if arguments is None:
            arguments = json.dumps(self.arguments, ensure_ascii=False, separators=(",", ":"))
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": arguments},
        }


class ProviderMessage(BaseModel):
    """A structured message exchanged with the chat-completions endpoint."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None

    # These fields are intentionally excluded from ``model_dump`` and trace
    # models.  ``to_provider_dict`` is the only normal path that emits them.
    reasoning_details: Any = Field(default=None, exclude=True, repr=False)
    reasoning: Any = Field(default=None, exclude=True, repr=False)
    reasoning_details_present: bool = Field(default=False, exclude=True, repr=False)
    reasoning_present: bool = Field(default=False, exclude=True, repr=False)

    def to_provider_dict(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.role == "assistant" and self.tool_calls:
            message["tool_calls"] = [call.to_provider_dict() for call in self.tool_calls]
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("tool messages require tool_call_id")
            message["tool_call_id"] = self.tool_call_id
        # Preserve exactly what the provider returned. In particular, do not
        # coerce an object/list into text or attempt to interpret it. Both
        # fields may coexist in a compatible OpenRouter response.
        if self.reasoning_details_present:
            message["reasoning_details"] = self.reasoning_details
        if self.reasoning_present:
            message["reasoning"] = self.reasoning
        return message


class AssistantTurn(BaseModel):
    """Normalised first choice from a model completion response."""

    model_config = ConfigDict(extra="forbid")

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    reasoning_details: Any = Field(default=None, exclude=True, repr=False)
    reasoning: Any = Field(default=None, exclude=True, repr=False)
    reasoning_details_present: bool = Field(default=False, exclude=True, repr=False)
    reasoning_present: bool = Field(default=False, exclude=True, repr=False)

    def to_provider_message(self) -> ProviderMessage:
        return ProviderMessage(
            role="assistant",
            content=self.content,
            tool_calls=self.tool_calls,
            reasoning_details=self.reasoning_details,
            reasoning=self.reasoning,
            reasoning_details_present=self.reasoning_details_present,
            reasoning_present=self.reasoning_present,
        )


class ToolExecution(BaseModel):
    """Observable result of one allow-listed tool execution."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: int = Field(default=0, ge=0)

    def to_provider_message(self) -> ProviderMessage:
        body: dict[str, Any] = {"ok": self.success}
        if self.success:
            body["result"] = self.result
        else:
            body["error"] = self.error or "tool execution failed"
        return ProviderMessage(
            role="tool",
            tool_call_id=self.tool_call_id,
            content=json.dumps(body, ensure_ascii=False, default=str),
        )


class ToolCallSummary(BaseModel):
    """Non-reasoning metadata from a requested tool call."""

    tool_call_id: str
    tool_name: str
    arguments_valid: bool


class TraceEvent(BaseModel):
    """A compact, in-memory audit event suitable for rendering in the UI."""

    model_config = ConfigDict(extra="forbid")

    step: int
    kind: Literal["model", "tool", "runtime"]
    created_at: datetime = Field(default_factory=utc_now)
    finish_reason: str | None = None
    content_present: bool | None = None
    content_characters: int | None = Field(default=None, ge=0)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    tool_execution: ToolExecution | None = None
    detail: str | None = None


class AgentTrace(BaseModel):
    """Trace excludes raw provider responses and all hidden reasoning payloads."""

    run_id: str
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    events: list[TraceEvent] = Field(default_factory=list)

    def add(self, event: TraceEvent) -> None:
        self.events.append(event)

    def finish(self) -> None:
        self.finished_at = utc_now()


class AgentRunResult(BaseModel):
    """Structured final value returned to the web/service layer."""

    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    answer: str
    trace: AgentTrace
    steps: int = Field(ge=0)
    error: str | None = None


def coerce_provider_message(value: ProviderMessage | dict[str, Any]) -> ProviderMessage:
    """Convert trusted history input into a typed provider message.

    This adapter makes the runtime convenient to use from FastAPI while
    retaining one canonical representation internally.
    """

    if isinstance(value, ProviderMessage):
        return value
    return ProviderMessage.model_validate(value)

"""A small, generic allow-list for travel tools.

The registry knows nothing about Tutu, a browser, a desktop, or a user
workspace.  Integration code injects only the bound travel handlers it wants
the model to call.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from .models import ToolCall, ToolExecution
from .security import redact_text


ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One model-visible function and its injected Python implementation."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    # For irreversible/handoff-style tools.  The initial search run does not
    # expose them; the UI must opt in after an explicit user action.
    requires_explicit_user_selection: bool = False
    # A successful call already produces the complete, deterministic result
    # for this run.  The runtime returns it immediately instead of asking the
    # model to interpret it and accidentally issuing duplicate low-level
    # searches.  This is deliberately opt-in per high-level tool.
    finishes_agent_run: bool = False

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(f"Invalid tool name: {self.name!r}")
        if not self.description.strip():
            raise ValueError(f"Tool {self.name!r} needs a description")
        if self.parameters.get("type") != "object":
            raise ValueError(f"Tool {self.name!r} parameters must be a JSON-schema object")

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Executes only explicitly registered functions.

    ``ToolRegistry`` is intentionally generic so application composition can
    register Tutu MCP, solver, and risk functions without coupling the runtime
    to their implementation modules.
    """

    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for definition in tools:
            self.register(definition)

    @classmethod
    def from_mapping(
        cls,
        definitions: Mapping[str, tuple[str, dict[str, Any], ToolHandler]],
    ) -> "ToolRegistry":
        """Convenience constructor for composition roots and small demos."""

        return cls(
            ToolDefinition(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
            )
            for name, (description, parameters, handler) in definitions.items()
        )

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool is already registered: {definition.name}")
        self._tools[definition.name] = definition

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def finishes_agent_run(self, name: str) -> bool:
        """Whether a successful call to ``name`` ends this agent run."""

        definition = self._tools.get(name)
        return bool(definition and definition.finishes_agent_run)

    def schemas(self, *, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        """Return OpenAI-compatible schemas for the permitted tools.

        A service can hide ``create_checkout_link`` until the UI has recorded
        an explicit selected tariff simply by supplying an ``allowed_names``
        set for that run.
        """

        if allowed_names is None:
            return [
                tool.to_openai_schema()
                for tool in self._tools.values()
                if not tool.requires_explicit_user_selection
            ]
        unknown = allowed_names.difference(self._tools)
        if unknown:
            raise ValueError(f"Unknown allowed tool(s): {', '.join(sorted(unknown))}")
        return [
            tool.to_openai_schema()
            for name, tool in self._tools.items()
            if name in allowed_names
        ]

    async def execute_call(
        self,
        call: ToolCall,
        *,
        allowed_names: set[str] | None = None,
    ) -> ToolExecution:
        """Execute a model request and turn every failure into tool feedback.

        An invalid or unavailable call does not crash the whole agent run; the
        model receives a structured error and can select a different tool.
        """

        started = time.perf_counter()

        def outcome(
            *,
            success: bool,
            result: Any = None,
            error: str | None = None,
        ) -> ToolExecution:
            return ToolExecution(
                tool_call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                success=success,
                result=_serialise_result(result),
                error=error,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

        if call.arguments_error:
            return outcome(success=False, error=f"Invalid JSON arguments: {call.arguments_error}")

        definition = self._tools.get(call.name)
        if definition is None:
            return outcome(success=False, error=f"Unknown tool: {call.name}")
        if allowed_names is None and definition.requires_explicit_user_selection:
            return outcome(
                success=False,
                error=f"Tool requires explicit user selection: {call.name}",
            )
        if allowed_names is not None and call.name not in allowed_names:
            return outcome(success=False, error=f"Tool is not allowed in this run: {call.name}")

        try:
            value = definition.handler(call.arguments)
            if inspect.isawaitable(value):
                value = await value
            return outcome(success=True, result=value)
        except Exception as exc:  # Deliberately feedback to model, not a crash.
            return outcome(success=False, error=redact_text(f"{type(exc).__name__}: {exc}"))

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        tool_call_id: str = "manual-tool-call",
        allowed_names: set[str] | None = None,
    ) -> ToolExecution:
        """Convenient non-model entry point, useful for explicit checkout UI."""

        return await self.execute_call(
            ToolCall(id=tool_call_id, name=name, arguments=arguments or {}),
            allowed_names=allowed_names,
        )


def _serialise_result(value: Any) -> Any:
    """Make tool output JSON-safe without changing normal dict/list values."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _serialise_result(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialise_result(item) for item in value]
    if isinstance(value, list):
        return [_serialise_result(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _serialise_result(asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Ensure the trace itself remains JSON-renderable even if a handler returns
    # a library-specific scalar object.
    return str(value)

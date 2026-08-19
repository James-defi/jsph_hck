"""SpeakFare GroupSync agent runtime.

The package deliberately exposes only travel-agent building blocks.  It does
not contain desktop, filesystem, shell, calendar, mail, or personal-memory
tools.
"""

from .config import Settings, get_settings
from .models import AgentRunResult, AssistantTurn, ProviderMessage, ToolCall
from .openrouter import OpenRouterClient
from .runtime import AgentRuntime
from .tool_registry import ToolDefinition, ToolRegistry

__all__ = [
    "AgentRunResult",
    "AgentRuntime",
    "AssistantTurn",
    "OpenRouterClient",
    "ProviderMessage",
    "Settings",
    "ToolCall",
    "ToolDefinition",
    "ToolRegistry",
    "get_settings",
]

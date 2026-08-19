"""Input trust boundary: normalize untrusted text and refuse high-confidence injection.

User text, chat history, LLM output and Tutu MCP payloads are untrusted data.
Only Python policy, Pydantic schemas, the solver and RunStore confer authority.
This module is an early filter, not the only defense.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Mapping


MAX_INPUT_CHARS = 6_000
INJECTION_REFUSAL = (
    "Я помогаю только спланировать поездку. Не выполняю команды вне этой задачи "
    "и не меняю свои правила по тексту в запросе."
)
TOO_LONG_MESSAGE = "Запрос слишком длинный: максимум 6000 символов."

_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060")
_ZERO_WIDTH_TABLE = dict.fromkeys(map(ord, "".join(_ZERO_WIDTH_CHARS)), None)

_REDACT_KEYS = {
    "checkout_ref",
    "checkout_url",
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "x-api-key",
    "openrouter_api_key",
    "concession_replan_context",
    "private_recipe",
}

_SECRET_IN_TEXT = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|checkout_ref)",
)

_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)\b",
        r"disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions",
        r"jailbreak",
        r"игнорируй\s+(?:все\s+)?предыдущ",
        r"забудь\s+(?:все\s+)?(?:предыдущие\s+)?(?:инструкции|правила|промпт)",
        r"<\s*\|?\s*system\s*\|?\s*>",
        r"<\s*/\s*system\s*>",
        r"\[\s*(?:system|developer)\s*\]",
        r"<\|im_start\|>\s*system",
        r"<<\s*SYS\s*>>",
        r"""['"]role['"]\s*:\s*['"](?:system|developer)['"]""",
        r"\brole\s*[:=]\s*['\"]?(?:system|developer)\b",
        r"system\s*prompt",
        r"системн\w*\s+промпт",
        r"checkout_ref",
        r"api[_-]?key",
        r"openrouter",
        r"(?:show|reveal|print|dump|выведи|покажи)\s+(?:your\s+|свой\s+)?(?:system\s+)?(?:prompt|instructions|ключ(?:и|а|ей)?|secrets?|промпт)",
        r"покажи\s+(?:свой\s+)?(?:системн|ключ|промпт|инструкц)",
        r"выведи\s+(?:свой\s+)?(?:системн|ключ|промпт|инструкц)",
        r"\b(?:bash|powershell|cmd\.exe|/bin/sh)\b",
        r"\brm\s+-rf\b",
        r"\b(?:curl|wget)\s+https?://",
        r"(?:run|execute)\s+(?:a\s+)?(?:shell|terminal|command)\b",
        r"выполни\s+(?:команду|shell|терминал)",
        r"(?:unknown|unauthori[sz]ed|запрещ\w+)\s+tool",
        r"вызови\s+(?:неизвестный|чужой|запрещ\w+)\s+инструмент",
    )
)


@dataclass(frozen=True)
class InputSecurityDecision:
    """Outcome of screening one untrusted string."""

    blocked: bool
    normalized_text: str
    reason: str | None = None
    refusal: str | None = None


def normalize_untrusted_text(value: Any) -> str:
    """NFKC-normalize and strip zero-width characters from untrusted input."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return text.translate(_ZERO_WIDTH_TABLE)


def redact_text(value: Any) -> str:
    """Replace secret-like fragments in a string destined for logs or traces."""

    return _SECRET_IN_TEXT.sub("[redacted]", str(value))


def redact_value(value: Any) -> Any:
    """Recursively drop secret keys and redact secret-like strings."""

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in _REDACT_KEYS or "checkout_ref" in name.lower():
                continue
            redacted[name] = redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class InputSecurityGate:
    """Early high-confidence filter for prompt injection and oversized input."""

    def __init__(self, *, max_chars: int = MAX_INPUT_CHARS) -> None:
        self.max_chars = max_chars

    def screen(self, text: Any) -> InputSecurityDecision:
        normalized = normalize_untrusted_text(text).strip()
        if len(normalized) > self.max_chars:
            return InputSecurityDecision(
                blocked=True,
                normalized_text=normalized,
                reason="too_long",
                refusal=TOO_LONG_MESSAGE,
            )
        if self.detect_injection(normalized):
            return InputSecurityDecision(
                blocked=True,
                normalized_text=normalized,
                reason="injection",
                refusal=INJECTION_REFUSAL,
            )
        return InputSecurityDecision(blocked=False, normalized_text=normalized)

    def detect_injection(self, text: str) -> bool:
        if not text:
            return False
        return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


def security_refusal_presentation(query: str, *, refusal: str = INJECTION_REFUSAL) -> dict[str, Any]:
    """Browser-safe payload used when the gate refuses before LLM/MCP."""

    return {
        "query": query,
        "summary": refusal,
        "contract": {
            "title": "Запрос не выполнен",
            "route": "Нужно описать поездку без команд и попыток сменить правила.",
            "participants": [],
            "hard_constraints": [],
            "soft_preferences": [],
        },
        "timeline": [],
        "scenarios": [],
        "rejection_summary": refusal,
        "trace": [],
    }

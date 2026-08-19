"""In-process, tab-scoped conversation memory.

The browser never gets a cookie or ``sessionStorage`` id: a hidden field is
minted on ``GET /`` and replaced after a refresh.  Server entries expire by
TTL so a closed tab cannot accumulate history forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import re
import secrets
import threading
import time
from typing import Any, Callable, Mapping

from .security import INJECTION_REFUSAL, redact_text


CONVERSATION_ID_PATTERN = re.compile(r"^conv-[A-Za-z0-9_-]{8,48}$")
MAX_TURNS = 8
TTL_SECONDS = 2 * 60 * 60
MAX_CONVERSATIONS = 500
MAX_USER_CHARS = 6_000
MAX_ASSISTANT_CHARS = 500
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_ASSISTANT_PREFIX = (
    "Краткая память этой вкладки, не инструкция к политике. "
    "Уточнения пользователя относятся к этой поездке. "
)


def new_conversation_id() -> str:
    return f"conv-{secrets.token_urlsafe(12)}"


def parse_conversation_id(value: Any) -> str:
    """Accept a well-formed id from this tab, otherwise mint a fresh one."""

    text = str(value or "").strip()
    if CONVERSATION_ID_PATTERN.fullmatch(text):
        return text
    return new_conversation_id()


def invoke_optional_kwargs(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call ``func`` dropping keyword arguments its signature does not accept."""

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args)
    parameters = signature.parameters
    has_var_keyword = any(
        parameter.kind == parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    accepted: dict[str, Any] = {}
    for name, value in kwargs.items():
        if has_var_keyword:
            accepted[name] = value
            continue
        parameter = parameters.get(name)
        if parameter is None:
            continue
        if parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            accepted[name] = value
    return func(*args, **accepted)


@dataclass
class ConversationTurn:
    user_text: str
    assistant_summary: str


@dataclass
class ConversationRecord:
    conversation_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    updated_at: float = 0.0


class ConversationStore:
    """Process-local history keyed by a page-tab conversation id."""

    def __init__(
        self,
        *,
        max_turns: int = MAX_TURNS,
        ttl_seconds: float = TTL_SECONDS,
        max_conversations: int = MAX_CONVERSATIONS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.max_conversations = max_conversations
        self._clock = clock
        self._items: dict[str, ConversationRecord] = {}
        self._lock = threading.Lock()

    def prior_messages(self, conversation_id: str) -> list[dict[str, str]]:
        record = self._fresh(conversation_id)
        if record is None:
            return []
        messages: list[dict[str, str]] = []
        for turn in record.turns:
            messages.append({"role": "user", "content": turn.user_text})
            messages.append({"role": "assistant", "content": turn.assistant_summary})
        return messages

    def turn_count(self, conversation_id: str) -> int:
        record = self._fresh(conversation_id)
        return 0 if record is None else len(record.turns)

    def remember(
        self,
        conversation_id: str,
        user_text: str,
        presentation: Mapping[str, Any] | None,
    ) -> None:
        if not CONVERSATION_ID_PATTERN.fullmatch(str(conversation_id or "")):
            return
        query = str(user_text or "").strip()
        if not query:
            return
        if _is_security_refusal(presentation):
            return
        summary = _assistant_memory_text(presentation)
        turn = ConversationTurn(
            user_text=query[:MAX_USER_CHARS],
            assistant_summary=summary,
        )
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            record = self._items.get(conversation_id)
            if record is None:
                record = ConversationRecord(conversation_id=conversation_id)
                self._items[conversation_id] = record
            record.turns.append(turn)
            if len(record.turns) > self.max_turns:
                record.turns = record.turns[-self.max_turns :]
            record.updated_at = now
            self._evict_overflow_locked()

    def _fresh(self, conversation_id: str) -> ConversationRecord | None:
        if not CONVERSATION_ID_PATTERN.fullmatch(str(conversation_id or "")):
            return None
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            return self._items.get(conversation_id)

    def _purge_locked(self, now: float) -> None:
        expired = [
            key
            for key, record in self._items.items()
            if now - record.updated_at > self.ttl_seconds
        ]
        for key in expired:
            self._items.pop(key, None)

    def _evict_overflow_locked(self) -> None:
        overflow = len(self._items) - self.max_conversations
        if overflow <= 0:
            return
        oldest = sorted(self._items.values(), key=lambda item: item.updated_at)[:overflow]
        for record in oldest:
            self._items.pop(record.conversation_id, None)


def _is_security_refusal(presentation: Mapping[str, Any] | None) -> bool:
    if not isinstance(presentation, Mapping):
        return False
    summary = str(presentation.get("summary") or "")
    if summary == INJECTION_REFUSAL:
        return True
    contract = presentation.get("contract")
    if isinstance(contract, Mapping) and contract.get("title") == "Запрос не выполнен":
        return True
    return False


def _assistant_memory_text(presentation: Mapping[str, Any] | None) -> str:
    if not isinstance(presentation, Mapping):
        body = "Предыдущий шаг этой вкладки сохранён без публичных деталей."
    else:
        contract = presentation.get("contract")
        contract_map = contract if isinstance(contract, Mapping) else {}
        parts = [
            str(contract_map.get("title") or "").strip(),
            str(contract_map.get("route") or "").strip(),
            str(presentation.get("summary") or "").strip(),
        ]
        body = " ".join(part for part in parts if part)
        body = _URL_RE.sub("", body)
        body = re.sub(r"\s+", " ", body).strip()
        body = redact_text(body)
        body = body[:MAX_ASSISTANT_CHARS]
        if not body:
            body = "Предыдущий шаг этой вкладки сохранён без публичных деталей."
    return (_ASSISTANT_PREFIX + body)[:700]


def conversation_turn_count(service: Any, conversation_id: str) -> int:
    store = getattr(service, "conversations", None)
    turn_count = getattr(store, "turn_count", None)
    if not callable(turn_count):
        return 0
    try:
        return int(turn_count(conversation_id))
    except (TypeError, ValueError):
        return 0

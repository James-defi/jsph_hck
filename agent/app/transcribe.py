"""OpenRouter speech-to-text with an explicit model fallback chain.

The chat-completions provider allow-list is intentionally not reused: STT
hosts (Groq, DeepInfra, Google) are a different catalogue from tool-calling
chat endpoints.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .openrouter import OpenRouterError


ALLOWED_AUDIO_FORMATS = frozenset({"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac", "mp4"})
DEFAULT_STT_MODELS = (
    "openai/whisper-large-v3-turbo",
    "google/chirp-3",
    "openai/whisper-large-v3",
)


class TranscriptionError(OpenRouterError):
    """A user-safe STT failure with no request secret or audio payload."""


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    model: str


class OpenRouterTranscriber:
    """POST ``/audio/transcriptions`` trying models in order until one succeeds."""

    def __init__(
        self,
        *,
        api_key: str,
        models: Sequence[str] = DEFAULT_STT_MODELS,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 45.0,
        max_audio_bytes: int = 4_000_000,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenRouter API key is required")
        ordered = tuple(model.strip() for model in models if str(model).strip())
        if not ordered:
            raise ValueError("at least one STT model is required")
        self.models = ordered
        self.base_url = base_url.rstrip("/")
        self.max_audio_bytes = max_audio_bytes
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = http_client is None

    @classmethod
    def from_settings(cls, settings: Any) -> "OpenRouterTranscriber":
        return cls(
            api_key=settings.require_openrouter_api_key(),
            models=settings.openrouter_stt_model_order,
            base_url=settings.openrouter_base_url,
            timeout_seconds=settings.openrouter_stt_timeout_seconds,
            max_audio_bytes=settings.openrouter_stt_max_audio_bytes,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def transcribe(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        language: str = "ru",
    ) -> TranscriptionResult:
        if not audio_bytes:
            raise TranscriptionError("empty audio")
        if len(audio_bytes) > self.max_audio_bytes:
            raise TranscriptionError("audio too large")
        fmt = normalize_audio_format(audio_format)
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        last_error = "all STT models failed"
        for model in self.models:
            payload = {
                "model": model,
                "language": language,
                "input_audio": {"data": encoded, "format": fmt},
            }
            try:
                response = await self._client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=self._headers,
                    json=payload,
                )
            except httpx.HTTPError:
                last_error = "transport error"
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                continue
            try:
                body = response.json()
            except json.JSONDecodeError:
                last_error = "invalid JSON"
                continue
            if not isinstance(body, Mapping):
                last_error = "unexpected body"
                continue
            text = _extract_transcript(body)
            if not text:
                last_error = "empty transcript"
                continue
            return TranscriptionResult(text=text, model=model)
        raise TranscriptionError(last_error)


def normalize_audio_format(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("audio/"):
        text = text.split("/", 1)[1]
    text = text.split(";", 1)[0].strip()
    aliases = {"x-m4a": "m4a", "mpeg": "mp3", "x-wav": "wav", "wave": "wav"}
    text = aliases.get(text, text)
    if text not in ALLOWED_AUDIO_FORMATS:
        raise TranscriptionError("unsupported audio format")
    return text


def decode_audio_base64(value: Any, *, max_bytes: int) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise TranscriptionError("empty audio")
    if text.lower().startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    # Base64 expands by ~4/3; reject obvious oversize strings before decode.
    if len(text) > max_bytes * 2:
        raise TranscriptionError("audio too large")
    try:
        audio = base64.b64decode(text, validate=False)
    except (ValueError, TypeError) as exc:
        raise TranscriptionError("invalid audio encoding") from exc
    if not audio:
        raise TranscriptionError("empty audio")
    if len(audio) > max_bytes:
        raise TranscriptionError("audio too large")
    return audio


def _extract_transcript(body: Mapping[str, Any]) -> str:
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    nested = body.get("output")
    if isinstance(nested, Mapping):
        inner = nested.get("text")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return ""

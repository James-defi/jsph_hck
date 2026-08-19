"""OpenRouter STT fallback chain: turbo → chirp-3 → whisper-large-v3."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.app.config import Settings
from agent.app.transcribe import (
    OpenRouterTranscriber,
    TranscriptionError,
    TranscriptionResult,
)
from agent.app.web import create_app


def test_stt_models_are_independent_of_chat_provider_allow_list() -> None:
    settings = Settings(
        openrouter_stt_models="openai/whisper-large-v3-turbo,google/chirp-3,openai/whisper-large-v3"
    )
    assert settings.openrouter_stt_model_order == (
        "openai/whisper-large-v3-turbo",
        "google/chirp-3",
        "openai/whisper-large-v3",
    )
    assert "google/chirp-3" not in settings.openrouter_provider_order
    with pytest.raises(ValidationError):
        Settings(openrouter_stt_models="")


def test_transcriber_falls_back_to_chirp_when_turbo_fails() -> None:
    models_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        models_seen.append(str(body["model"]))
        assert "provider" not in body
        assert body["language"] == "ru"
        assert body["input_audio"]["format"] == "webm"
        if body["model"] == "openai/whisper-large-v3-turbo":
            return httpx.Response(503, json={"error": "busy"})
        if body["model"] == "google/chirp-3":
            return httpx.Response(200, json={"text": "Нас трое в Стамбул"})
        return httpx.Response(500, json={"error": "unused"})

    transcriber = OpenRouterTranscriber(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = asyncio.run(
            transcriber.transcribe(audio_bytes=b"fake-webm", audio_format="audio/webm;codecs=opus")
        )
    finally:
        asyncio.run(transcriber._client.aclose())

    assert result.text == "Нас трое в Стамбул"
    assert result.model == "google/chirp-3"
    assert models_seen == ["openai/whisper-large-v3-turbo", "google/chirp-3"]


def test_transcriber_uses_whisper_v3_when_earlier_models_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "openai/whisper-large-v3":
            return httpx.Response(200, json={"text": "Автобус Казань Москва"})
        return httpx.Response(400, json={"error": "unsupported format"})

    transcriber = OpenRouterTranscriber(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = asyncio.run(transcriber.transcribe(audio_bytes=b"fake-webm", audio_format="webm"))
    finally:
        asyncio.run(transcriber._client.aclose())

    assert result.model == "openai/whisper-large-v3"
    assert "Казань" in result.text


def test_transcriber_raises_when_all_models_fail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    transcriber = OpenRouterTranscriber(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(TranscriptionError):
            asyncio.run(transcriber.transcribe(audio_bytes=b"fake", audio_format="wav"))
    finally:
        asyncio.run(transcriber._client.aclose())


class _FakeTranscriber:
    max_audio_bytes = 4_000_000

    async def transcribe(self, *, audio_bytes: bytes, audio_format: str, language: str = "ru"):
        assert audio_bytes
        assert language == "ru"
        return TranscriptionResult(text="Нас трое в Стамбул", model="openai/whisper-large-v3-turbo")


def test_transcribe_endpoint_fills_text_without_running_search() -> None:
    client = TestClient(create_app(transcriber=_FakeTranscriber()))
    response = client.post(
        "/api/transcribe",
        json={
            "audio_base64": base64.b64encode(b"RIFF....").decode("ascii"),
            "format": "wav",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"text": "Нас трое в Стамбул"}

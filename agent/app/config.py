"""Configuration for the bounded SpeakFare travel agent.

Secrets are loaded from ``agent/.env`` locally and are never embedded in
source code or returned in the agent trace.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


AGENT_ROOT = Path(__file__).resolve().parents[1]

# Checked against OpenRouter's endpoint catalogue on 2026-08-19.  Every
# provider here advertises support for the three request features this agent
# needs: ``reasoning``, ``tools`` and ``tool_choice``.  Keeping an explicit
# allow-list is intentional: automatic routing must not silently send a
# travel request to an arbitrary new endpoint.
VETTED_OPENROUTER_PROVIDERS = (
    "baseten",
    "deepinfra",
    "fireworks",
    "streamlake",
)


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and ``agent/.env``."""

    model_config = SettingsConfigDict(
        env_file=AGENT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    openrouter_api_key: str = Field(default="", repr=False)
    openrouter_model: str = "deepseek/deepseek-v4-flash-0731"
    # Baseten remains the preferred endpoint.  The client sends this list as
    # both OpenRouter's ``order`` and strict ``only`` allow-list, so failover
    # stays inside this explicitly vetted set.  A comma-separated string is
    # used rather than a list so it is simple and unambiguous in a .env file.
    openrouter_providers: str = ",".join(VETTED_OPENROUTER_PROVIDERS)
    openrouter_allow_fallbacks: bool = True
    openrouter_require_parameters: bool = True
    openrouter_parallel_tool_calls: bool = False
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    openrouter_reasoning_enabled: bool = True
    # Additional attempts after an initial 429/5xx response.  Completion
    # retries happen before the runtime can execute any model-requested tool.
    openrouter_max_retries: int = Field(default=2, ge=0, le=5)
    openrouter_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=30.0)
    openrouter_max_retry_delay_seconds: float = Field(default=8.0, ge=0.0, le=60.0)
    # STT is a separate catalogue from tool-calling chat.  Do not reuse
    # OPENROUTER_PROVIDERS here: Whisper/Chirp hosts are not the vetted
    # reasoning+tools allow-list.
    openrouter_stt_models: str = (
        "openai/whisper-large-v3-turbo,google/chirp-3,openai/whisper-large-v3"
    )
    openrouter_stt_timeout_seconds: float = Field(default=45.0, ge=1.0, le=120.0)
    openrouter_stt_max_audio_bytes: int = Field(default=4_000_000, ge=10_000, le=25_000_000)

    tutu_mcp_url: str = "https://mcp.tutu.ru/mcp"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    agent_max_steps: int = Field(default=20, ge=1, le=24)

    @field_validator("openrouter_base_url", "tutu_mcp_url", mode="before")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return str(value).rstrip("/")

    @field_validator("openrouter_providers")
    @classmethod
    def _providers_are_vetted(cls, value: str) -> str:
        providers = tuple(
            provider.strip().lower()
            for provider in str(value).split(",")
            if provider.strip()
        )
        if not providers:
            raise ValueError("OPENROUTER_PROVIDERS must contain at least one provider")
        if len(set(providers)) != len(providers):
            raise ValueError("OPENROUTER_PROVIDERS must not contain duplicates")
        unsupported = sorted(set(providers).difference(VETTED_OPENROUTER_PROVIDERS))
        if unsupported:
            raise ValueError(
                "OPENROUTER_PROVIDERS contains providers not vetted for "
                "reasoning + tools + tool_choice: " + ", ".join(unsupported)
            )
        return ",".join(providers)

    @field_validator("openrouter_stt_models")
    @classmethod
    def _stt_models_are_present(cls, value: str) -> str:
        models = tuple(model.strip() for model in str(value).split(",") if model.strip())
        if not models:
            raise ValueError("OPENROUTER_STT_MODELS must contain at least one model")
        if len(set(models)) != len(models):
            raise ValueError("OPENROUTER_STT_MODELS must not contain duplicates")
        return ",".join(models)

    @property
    def openrouter_provider_order(self) -> tuple[str, ...]:
        """The exact, validated provider order sent to OpenRouter."""

        return tuple(self.openrouter_providers.split(","))

    @property
    def openrouter_stt_model_order(self) -> tuple[str, ...]:
        return tuple(self.openrouter_stt_models.split(","))

    @property
    def chat_completions_url(self) -> str:
        return f"{self.openrouter_base_url}/chat/completions"

    def require_openrouter_api_key(self) -> str:
        """Return the configured key or raise without revealing any secret."""

        if not self.openrouter_api_key.strip():
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        return self.openrouter_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings once per process."""

    return Settings()

"""Executable live SpeakFare application.

Run from ``agent/`` with::

    uvicorn app.main:app --reload
"""

from .application import build_live_service
from .config import get_settings
from .transcribe import OpenRouterTranscriber
from .web import create_app


def _build_app():
    settings = get_settings()
    transcriber = None
    if settings.openrouter_api_key.strip():
        transcriber = OpenRouterTranscriber.from_settings(settings)
    return create_app(build_live_service(settings=settings), transcriber=transcriber)


app = _build_app()

"""FastAPI presentation layer for the SpeakFare GroupSync MVP.

The web layer deliberately knows nothing about OpenRouter, MCP transport, or
the GroupSync solver.  A real runtime is passed to :func:`create_app` as a
small service object, while :class:`DemoGroupSyncService` makes the whole page
usable and testable without a model key or network connection.

Expected injected-service contract (methods can be sync or async)::

    run(user_text: str) -> mapping | pydantic/dataclass result
    create_checkout_link(run_id: str, component_ref: str, variant_id: str) -> mapping
    replan_concession(run_id: str, proposed_max_wait_minutes: int) -> mapping

``run`` may accept an optional ``conversation_id`` for tab-scoped memory.
``create_checkout_link`` is only invoked by the browser after the user has
selected a precise fare.  The UI calls it a handoff link, never a booking.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import logging
import secrets
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .conversation import (
    ConversationStore,
    conversation_turn_count as stored_turn_count,
    invoke_optional_kwargs,
    new_conversation_id,
    parse_conversation_id,
)
from .geo import resolve_place
from .security import (
    TOO_LONG_MESSAGE,
    InputSecurityGate,
    redact_text,
    redact_value,
    security_refusal_presentation,
)
from .transcribe import TranscriptionError, decode_audio_base64, normalize_audio_format


LOGGER = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


@runtime_checkable
class GroupSyncWebService(Protocol):
    """Minimal boundary between the UI and an agent/runtime implementation."""

    def run(self, user_text: str) -> Any:
        """Run a text request and return a serialisable GroupSync result."""

    def create_checkout_link(self, run_id: str, component_ref: str, variant_id: str) -> Any:
        """Return a checkout handoff only for an explicitly selected fare."""

    def replan_concession(self, run_id: str, proposed_max_wait_minutes: int) -> Any:
        """Re-run exactly one server-stored safe concession."""


class DemoGroupSyncService:
    """Network-free data source used for the first screen and smoke tests.

    The data models the expected UI contract, not a promise about live Tutu
    availability.  Demo links intentionally point to the public Tutu aviation
    search page and are labelled as a search redirect.
    """

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self.conversations = ConversationStore()

    async def run(self, user_text: str, conversation_id: str | None = None) -> dict[str, Any]:
        run_id = f"run-{secrets.token_urlsafe(6)}"
        payload = _demo_payload(run_id=run_id, query=user_text)
        self._runs[run_id] = copy.deepcopy(payload)
        if conversation_id:
            self.conversations.remember(conversation_id, user_text, payload)
        return payload

    async def preview(self) -> dict[str, Any]:
        """Return an interactive, stored preview for the app's first screen."""

        run_id = "demo-preview"
        payload = _demo_payload(
            run_id=run_id,
            query="Трое летим из Москвы, Петербурга и Екатеринбурга в Лондон через IST.",
        )
        self._runs[run_id] = copy.deepcopy(payload)
        return payload

    async def create_checkout_link(
        self,
        run_id: str,
        component_ref: str,
        variant_id: str,
    ) -> dict[str, str]:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError("Не найден запуск. Выполните поиск заново.")

        matched_unit = next(
            (
                unit
                for scenario in run.get("scenarios", [])
                for unit in scenario.get("booking_units", [])
                if unit.get("component_ref") == component_ref
            ),
            None,
        )
        if matched_unit is None:
            raise ValueError("Компонент поездки не относится к этому подбору.")
        matched_tariff = next(
            (
                tariff
                for tariff in matched_unit.get("tariffs", [])
                if tariff.get("variant_id") == variant_id
            ),
            None,
        )
        if matched_tariff is None:
            raise ValueError("Тариф не относится к выбранному компоненту поездки.")

        # The client never sends this opaque original checkout ref.  A real
        # RunStore follows the same pattern: it resolves it server-side from
        # (run_id, component_ref, variant_id) before talking to Tutu MCP.
        _original_checkout_ref = matched_tariff["checkout_ref"]

        return {
            "url": "https://www.tutu.ru/avia/",
            "handoff_kind": "search_redirect",
            "message": (
                "Откроется выдача Туту. Перед оплатой проверьте рейс, стоимость "
                "и число пассажиров."
            ),
        }

    async def replan_concession(
        self,
        run_id: str,
        proposed_max_wait_minutes: int,
    ) -> dict[str, Any]:
        """Keep the offline demo's contract as strict as the live endpoint.

        The stock demo has no concession card, but test/demo subclasses may
        persist one.  Never accept an arbitrary maximum or recover a recipe
        from the editable search input.
        """

        run = self._runs.get(run_id)
        if run is None:
            raise KeyError("Не найден запуск. Выполните поиск заново.")
        proposal = run.get("constraint_negotiator")
        if not isinstance(proposal, Mapping):
            raise ValueError("Для этого запуска нет доступной уступки.")
        try:
            expected = int(proposal["to_max_wait_minutes"])
            received = int(proposed_max_wait_minutes)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Некорректный максимум ожидания.") from exc
        if expected != received:
            raise ValueError("Можно подтвердить только предложенную сервером уступку.")

        # A new run deliberately contains fresh demo data and a new id; no
        # old tariff or checkout_ref is carried across this boundary.
        payload = _demo_payload(
            run_id=f"run-{secrets.token_urlsafe(6)}",
            query=str(run.get("query") or "Групповая поездка"),
        )
        self._runs[payload["run_id"]] = copy.deepcopy(payload)
        return payload


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """Publish a lightweight event-loop heartbeat for the liveness probe.

    A wedged-but-alive loop stops updating the timestamp, so ``/healthz`` can
    tell a hung process apart from a healthy one without any external calls.
    """

    heartbeat = {"beat_at": time.monotonic()}

    async def beat() -> None:
        while True:
            heartbeat["beat_at"] = time.monotonic()
            await asyncio.sleep(0.5)

    task = asyncio.create_task(beat())
    app.state.event_loop_heartbeat = heartbeat
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(
    service: GroupSyncWebService | Any | None = None,
    *,
    transcriber: Any | None = None,
) -> FastAPI:
    """Build an app with an injected runtime, or the offline demo by default."""

    app = FastAPI(
        title="SpeakFare GroupSync",
        version="0.1.0",
        description="Text-first group rendezvous assistant with explicit checkout handoff.",
        lifespan=_lifespan,
    )
    app.state.group_sync_service = service or DemoGroupSyncService()
    app.state.transcriber = transcriber
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render_page(
        request: Request,
        *,
        result: Mapping[str, Any] | None = None,
        query: str = "",
        error: str | None = None,
        demo: bool = False,
        conversation_id: str | None = None,
        conversation_turn_count: int = 0,
    ) -> HTMLResponse:
        cid = conversation_id or new_conversation_id()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "result": result,
                "query": query,
                "error": error,
                "demo": demo,
                "conversation_id": cid,
                "conversation_turn_count": conversation_turn_count,
            },
        )

    def _page_turns(conversation_id: str) -> int:
        return stored_turn_count(app.state.group_sync_service, conversation_id)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        # A visible demo makes the UX reviewable immediately; it is only shown
        # with our offline service so every preview tariff can be validated.
        # A production injected service starts with a clean text-input page.
        active_service = app.state.group_sync_service
        if isinstance(active_service, DemoGroupSyncService):
            conversation_id = new_conversation_id()
            await active_service.preview()
            return render_page(request, conversation_id=conversation_id)
        return render_page(request, conversation_id=new_conversation_id())

    @app.post("/search", response_class=HTMLResponse)
    async def search_page(request: Request) -> HTMLResponse:
        payload = await _read_request_payload(request)
        conversation_id = parse_conversation_id(payload.get("conversation_id"))
        query = _clean_query(payload.get("query", ""))
        if not query:
            return render_page(
                request,
                query="",
                error="Опишите поездку текстом: кто, откуда, где встречаетесь и куда едете дальше.",
                conversation_id=conversation_id,
                conversation_turn_count=_page_turns(conversation_id),
            )
        screened = _screen_user_query(query)
        if screened.blocked:
            if screened.reason == "too_long":
                return render_page(
                    request,
                    query=query,
                    error=TOO_LONG_MESSAGE,
                    conversation_id=conversation_id,
                    conversation_turn_count=_page_turns(conversation_id),
                )
            if not screened.normalized_text:
                return render_page(
                    request,
                    query="",
                    error="Опишите поездку текстом: кто, откуда, где встречаетесь и куда едете дальше.",
                    conversation_id=conversation_id,
                    conversation_turn_count=_page_turns(conversation_id),
                )
            return render_page(
                request,
                result=_public_result(
                    security_refusal_presentation(query, refusal=screened.refusal or TOO_LONG_MESSAGE),
                    query=query,
                    conversation_id=conversation_id,
                ),
                query=query,
                conversation_id=conversation_id,
                conversation_turn_count=_page_turns(conversation_id),
            )
        if not screened.normalized_text:
            return render_page(
                request,
                query="",
                error="Опишите поездку текстом: кто, откуда, где встречаетесь и куда едете дальше.",
                conversation_id=conversation_id,
                conversation_turn_count=_page_turns(conversation_id),
            )
        query = screened.normalized_text

        try:
            raw_result = await _run_service(
                app.state.group_sync_service,
                query,
                conversation_id=conversation_id,
            )
            result = _public_result(raw_result, query=query, conversation_id=conversation_id)
        except Exception as exc:  # Do not put provider/MCP internals or secrets in the page.
            LOGGER.error("GroupSync search failed: %s", type(exc).__name__)
            return render_page(
                request,
                query=query,
                error="Агент пока не смог собрать проверяемый сценарий. Попробуйте ещё раз.",
                conversation_id=conversation_id,
                conversation_turn_count=_page_turns(conversation_id),
            )
        return render_page(
            request,
            result=result,
            query=query,
            conversation_id=conversation_id,
            conversation_turn_count=_page_turns(conversation_id),
        )

    @app.post("/api/search")
    async def search_api(request: Request) -> JSONResponse:
        payload = await _read_request_payload(request)
        conversation_id = parse_conversation_id(payload.get("conversation_id"))
        query = _clean_query(payload.get("query", ""))
        if not query:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Поле query не должно быть пустым.",
            )
        screened = _screen_user_query(query)
        if screened.blocked:
            if screened.reason == "too_long":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=TOO_LONG_MESSAGE,
                )
            if not screened.normalized_text and screened.reason != "injection":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Поле query не должно быть пустым.",
                )
            return JSONResponse(
                _public_result(
                    security_refusal_presentation(query, refusal=screened.refusal or TOO_LONG_MESSAGE),
                    query=query,
                    conversation_id=conversation_id,
                )
            )
        if not screened.normalized_text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Поле query не должно быть пустым.",
            )
        query = screened.normalized_text

        try:
            raw_result = await _run_service(
                app.state.group_sync_service,
                query,
                conversation_id=conversation_id,
            )
            result = _public_result(raw_result, query=query, conversation_id=conversation_id)
        except Exception as exc:
            LOGGER.error("GroupSync API search failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Не удалось получить проверяемый ответ агента.",
            ) from None
        return JSONResponse(result)

    @app.post("/api/checkout")
    async def checkout_api(request: Request) -> JSONResponse:
        payload = await _read_request_payload(request)
        run_id = str(payload.get("run_id", "")).strip()
        component_ref = str(payload.get("component_ref", "")).strip()
        variant_id = str(payload.get("variant_id", "")).strip()
        if not run_id or not component_ref or not variant_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Сначала выберите точный тариф.",
            )

        try:
            raw_handoff = await _create_checkout(
                app.state.group_sync_service,
                run_id=run_id,
                component_ref=component_ref,
                variant_id=variant_id,
            )
            handoff = _normalise_handoff(raw_handoff)
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=redact_text(str(exc)),
            ) from exc
        except Exception as exc:
            LOGGER.error("Checkout handoff failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Не удалось подготовить ссылку на Туту. Тариф не был забронирован.",
            ) from None
        return JSONResponse(handoff)

    @app.post("/concession/replan", response_class=HTMLResponse)
    async def concession_replan_page(request: Request) -> HTMLResponse:
        """Submit the card as a real POST, never as an editable chat phrase."""

        payload = await _read_request_payload(request)
        conversation_id = parse_conversation_id(payload.get("conversation_id"))
        run_id, proposed_max_wait_minutes = _concession_request_fields(payload)
        try:
            raw_result = await _replan_concession(
                app.state.group_sync_service,
                run_id=run_id,
                proposed_max_wait_minutes=proposed_max_wait_minutes,
            )
            result = _public_result(raw_result, query="", conversation_id=conversation_id)
        except (KeyError, ValueError) as exc:
            return render_page(
                request,
                error=redact_text(str(exc)),
                query="",
                conversation_id=conversation_id,
                conversation_turn_count=_page_turns(conversation_id),
            )
        except Exception as exc:
            LOGGER.error("Safe concession replan failed: %s", type(exc).__name__)
            return render_page(
                request,
                error="Не удалось выполнить новый проверяемый поиск. Старые тарифы не были использованы.",
                query="",
                conversation_id=conversation_id,
                conversation_turn_count=_page_turns(conversation_id),
            )
        return render_page(
            request,
            result=result,
            query=str(result.get("query") or ""),
            conversation_id=conversation_id,
            conversation_turn_count=_page_turns(conversation_id),
        )

    @app.post("/api/concession/replan")
    async def concession_replan_api(request: Request) -> JSONResponse:
        """JSON twin of the form action for API clients and integration tests."""

        payload = await _read_request_payload(request)
        conversation_id = parse_conversation_id(payload.get("conversation_id"))
        run_id, proposed_max_wait_minutes = _concession_request_fields(payload)
        try:
            raw_result = await _replan_concession(
                app.state.group_sync_service,
                run_id=run_id,
                proposed_max_wait_minutes=proposed_max_wait_minutes,
            )
            result = _public_result(raw_result, query="", conversation_id=conversation_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=redact_text(str(exc)),
            ) from exc
        except Exception as exc:
            LOGGER.error("Safe concession API replan failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Не удалось выполнить новый проверяемый поиск.",
            ) from None
        return JSONResponse(result)

    @app.post("/api/transcribe")
    async def transcribe_api(request: Request) -> JSONResponse:
        """Turn a short mic recording into composer text. Does not submit a search."""

        transcriber = app.state.transcriber
        if transcriber is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Диктовка сейчас недоступна.",
            )
        payload = await _read_request_payload(request)
        max_bytes = int(getattr(transcriber, "max_audio_bytes", 4_000_000))
        try:
            audio_format = normalize_audio_format(
                payload.get("format") or payload.get("audio_format") or "webm"
            )
            audio_bytes = decode_audio_base64(
                payload.get("audio_base64") or payload.get("data"),
                max_bytes=max_bytes,
            )
            transcribed = await transcriber.transcribe(
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                language="ru",
            )
        except TranscriptionError as exc:
            raise HTTPException(
                status_code=_stt_status_code(exc),
                detail=_stt_user_message(exc),
            ) from None
        except Exception as exc:
            LOGGER.error("Transcription failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Не удалось распознать речь. Попробуйте ещё раз.",
            ) from None
        text = str(getattr(transcribed, "text", transcribed) or "").strip()[:6_000]
        if not text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Не удалось разобрать речь. Попробуйте ещё раз.",
            )
        return JSONResponse({"text": text})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": type(app.state.group_sync_service).__name__}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness probe: reports the event-loop heartbeat age, not a constant."""

        heartbeat = getattr(app.state, "event_loop_heartbeat", None)
        if heartbeat is None:
            # A bare TestClient never runs the lifespan.  Still prove the loop
            # schedules and resumes this coroutine before answering.
            await asyncio.sleep(0)
            return JSONResponse({"status": "ok", "heartbeat": "unavailable"})
        age = time.monotonic() - float(heartbeat.get("beat_at", 0.0))
        if age > 5.0:
            return JSONResponse(
                {"status": "unhealthy", "heartbeat_age_seconds": round(age, 3)},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse({"status": "ok", "heartbeat_age_seconds": round(age, 3)})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness deploy-gate: config shape only; never calls OpenRouter/Tutu."""

        service = app.state.group_sync_service
        checks = {
            "service_contract": all(
                callable(getattr(service, name, None))
                for name in ("run", "create_checkout_link", "replan_concession")
            ),
        }
        ok = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return app


async def _read_request_payload(request: Request) -> dict[str, Any]:
    """Read JSON and standard urlencoded forms without requiring multipart."""

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректный JSON.") from exc
        return dict(data) if isinstance(data, Mapping) else {}

    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _clean_query(value: Any) -> str:
    query = str(value).strip()
    if len(query) > 6_000:
        raise HTTPException(status_code=422, detail="Запрос слишком длинный: максимум 6000 символов.")
    return query


def _screen_user_query(query: str):
    """Refuse high-confidence injection before any LLM or MCP call."""

    return InputSecurityGate().screen(query)


async def _run_service(service: Any, query: str, conversation_id: str | None = None) -> Any:
    """Call the simplest stable boundary: AgentRuntime.run(text)."""

    for method_name in ("run", "search", "plan"):
        method = getattr(service, method_name, None)
        if callable(method):
            return await _await_if_needed(
                invoke_optional_kwargs(method, query, conversation_id=conversation_id)
            )
    raise TypeError("Injected GroupSync service must implement run(user_text).")


async def _create_checkout(
    service: Any,
    *,
    run_id: str,
    component_ref: str,
    variant_id: str,
) -> Any:
    for method_name in ("create_checkout_link", "checkout", "create_handoff"):
        method = getattr(service, method_name, None)
        if not callable(method):
            continue
        try:
            result = method(
                run_id=run_id,
                component_ref=component_ref,
                variant_id=variant_id,
            )
        except TypeError:
            result = method(run_id, component_ref, variant_id)
        return await _await_if_needed(result)
    raise TypeError("Injected GroupSync service cannot create checkout handoffs.")


def _concession_request_fields(payload: Mapping[str, Any]) -> tuple[str, Any]:
    """Read the two fields allowed at the browser-to-replan boundary."""

    run_id = str(payload.get("run_id", "")).strip()
    proposed_max_wait_minutes = payload.get("proposed_max_wait_minutes")
    if not run_id or proposed_max_wait_minutes is None or str(proposed_max_wait_minutes).strip() == "":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Нужны идентификатор запуска и предложенный максимум ожидания.",
        )
    return run_id, proposed_max_wait_minutes


async def _replan_concession(
    service: Any,
    *,
    run_id: str,
    proposed_max_wait_minutes: Any,
) -> Any:
    """Call the explicitly named safe-replan capability if the service has it."""

    for method_name in ("replan_concession", "replan_one_concession"):
        method = getattr(service, method_name, None)
        if not callable(method):
            continue
        try:
            result = method(
                run_id=run_id,
                proposed_max_wait_minutes=proposed_max_wait_minutes,
            )
        except TypeError:
            result = method(run_id, proposed_max_wait_minutes)
        return await _await_if_needed(result)
    raise TypeError("Injected GroupSync service cannot replan a safe concession.")


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _to_jsonable(value: Any) -> Any:
    """Accept pydantic/dataclass runtime values without coupling to their types."""

    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _to_jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _public_result(raw_result: Any, *, query: str, conversation_id: str) -> dict[str, Any]:
    result = _normalise_result(raw_result, query=query)
    result["conversation_id"] = conversation_id
    return result


def _stt_user_message(exc: TranscriptionError) -> str:
    reason = str(exc)
    mapping = {
        "empty audio": "Не удалось прочитать запись. Попробуйте ещё раз.",
        "audio too large": "Запись слишком длинная: сократите диктовку.",
        "unsupported audio format": "Этот формат записи не поддерживается.",
        "invalid audio encoding": "Некорректная аудиозапись.",
    }
    return mapping.get(reason, "Не удалось распознать речь. Попробуйте ещё раз.")


def _stt_status_code(exc: TranscriptionError) -> int:
    if str(exc) in {
        "empty audio",
        "audio too large",
        "unsupported audio format",
        "invalid audio encoding",
    }:
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_503_SERVICE_UNAVAILABLE


def _normalise_result(raw_result: Any, *, query: str) -> dict[str, Any]:
    """Provide a safe minimum page even while a runtime returns a narrow result.

    A full runtime should return the same keys as the demo payload.  We do not
    invent facts missing from a real result: absent sections render as empty,
    and any model text remains escaped by Jinja autoescaping.
    """

    data = _to_jsonable(raw_result)
    if not isinstance(data, Mapping):
        data = {"summary": str(data)}
    else:
        data = dict(data)

    # AgentRunResult-style wrappers commonly keep the presentable payload in
    # ``result``/``presentation`` and the trace alongside it.
    nested = data.get("presentation") or data.get("result")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key in ("run_id", "trace", "tool_trace", "final_answer", "answer", "summary"):
            if key in data and key not in merged:
                merged[key] = data[key]
        data = merged

    data.pop("conversation_id", None)
    data.setdefault("run_id", f"run-{secrets.token_urlsafe(6)}")
    data.setdefault("query", query)
    data.setdefault("summary", data.get("final_answer") or data.get("answer") or "Агент завершил подбор.")
    data.setdefault(
        "contract",
        {
            "title": "Договор поездки ещё не извлечён",
            "route": "Нужно уточнить структуру поездки",
            "participants": [],
            "hard_constraints": [],
            "soft_preferences": [],
        },
    )
    data.setdefault("timeline", [])
    data.setdefault("scenarios", [])
    data.setdefault("rejection_summary", "")
    data["trace"] = _normalise_trace(data.get("trace") or data.get("tool_trace") or [])
    return redact_value(_to_jsonable(data))


def _normalise_trace(raw_trace: Any) -> list[dict[str, str]]:
    """Make AgentTrace compact and safe for UI display.

    We surface tool names and short execution state, never provider reasoning,
    raw tool payloads, or exception internals.
    """

    value = _to_jsonable(raw_trace)
    if isinstance(value, Mapping):
        value = value.get("events", value.get("items", []))
    if not isinstance(value, list):
        return []

    visible: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            visible.append({"tool": "agent", "summary": str(item)})
            continue
        execution = item.get("tool_execution")
        if isinstance(execution, Mapping):
            name = str(execution.get("tool_name") or "tool")
            outcome = "получены данные" if execution.get("success") else "инструмент ответил ошибкой"
            visible.append({"tool": name, "summary": outcome})
            continue
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, Mapping):
                    visible.append(
                        {
                            "tool": str(call.get("tool_name") or "tool"),
                            "summary": "агент запросил инструмент",
                        }
                    )
            continue
        if item.get("detail"):
            visible.append({"tool": "agent", "summary": "шаг выполнения завершён"})
            continue
        visible.append(
            {
                "tool": str(item.get("tool") or item.get("name") or "agent"),
                "summary": str(item.get("summary") or item.get("message") or "выполнен шаг"),
            }
        )
    return visible


def _normalise_handoff(raw_handoff: Any) -> dict[str, str]:
    data = _to_jsonable(raw_handoff)
    if isinstance(data, str):
        data = {"url": data}
    if not isinstance(data, Mapping):
        raise ValueError("Сервис вернул некорректную ссылку на Туту.")

    url = str(data.get("url") or data.get("checkout_url") or "").strip()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    is_tutu_host = host == "tutu.ru" or host.endswith(".tutu.ru")
    if parsed.scheme not in {"https", "http"} or not is_tutu_host:
        raise ValueError("Сервис вернул небезопасную ссылку на оформление.")

    kind = str(data.get("handoff_kind") or data.get("kind") or "deeplink")
    if kind not in {"deeplink", "search_redirect"}:
        kind = "deeplink"
    message = str(
        data.get("message")
        or "Проверьте рейс, стоимость и пассажиров на стороне Туту перед оплатой."
    )
    return {"url": url, "handoff_kind": kind, "message": message}


def _demo_timeline_leg(
    time: str,
    route: str,
    origin_code: str,
    destination_code: str,
    *,
    mode: str = "Самолёт",
) -> dict[str, Any]:
    clocks = str(time).replace("—", "–").replace("-", "–")
    if "–" in clocks:
        departure, arrival = (part.strip() for part in clocks.split("–", 1))
    else:
        departure, arrival = clocks, ""
    line = f"{origin_code} {departure} → {destination_code} {arrival}".strip()
    if "общ" in route.lower():
        line += " · общий рейс"
    leg: dict[str, Any] = {"time": time, "route": route, "line": line, "mode": mode}
    origin = resolve_place(origin_code, mode=mode)
    destination = resolve_place(destination_code, mode=mode)
    if origin is not None:
        leg["origin"] = origin.to_dict()
    if destination is not None:
        leg["destination"] = destination.to_dict()
    return leg


def _demo_payload(*, run_id: str, query: str) -> dict[str, Any]:
    """A static, evidence-labelled result for interface review and tests."""

    common_tariffs = [
        {
            "name": "Light",
            "price": "18 420 ₽",
            "variant_id": "common-light-v1",
            "checkout_ref": "common-light",
            "details": ["ручная кладь 8 кг", "багаж: нужно проверить", "невозвратный"],
            "state": "unknown",
        },
        {
            "name": "Flex",
            "price": "27 900 ₽",
            "variant_id": "common-flex-v1",
            "checkout_ref": "common-flex",
            "details": ["багаж: 1 место", "изменение: по правилам тарифа", "общий сегмент для 3 человек"],
            "state": "confirmed",
        },
    ]
    moscow_tariffs = [
        {
            "name": "Economy Basic",
            "price": "11 680 ₽",
            "variant_id": "moscow-basic-v1",
            "checkout_ref": "moscow-basic",
            "details": ["ручная кладь", "без заявленного багажа", "отдельное оформление"],
            "state": "unknown",
        },
        {
            "name": "Economy Plus",
            "price": "15 240 ₽",
            "variant_id": "moscow-plus-v1",
            "checkout_ref": "moscow-plus",
            "details": ["багаж: 1 место", "обмен: уточнить на Туту", "отдельное оформление"],
            "state": "confirmed",
        },
    ]
    petersburg_tariffs = [
        {
            "name": "Economy Basic",
            "price": "12 050 ₽",
            "variant_id": "petersburg-basic-v1",
            "checkout_ref": "petersburg-basic",
            "details": ["ручная кладь", "багаж: нужно проверить", "отдельное оформление"],
            "state": "unknown",
        },
        {
            "name": "Economy Plus",
            "price": "16 120 ₽",
            "variant_id": "petersburg-plus-v1",
            "checkout_ref": "petersburg-plus",
            "details": ["багаж: 1 место", "изменение: уточнить на Туту", "отдельное оформление"],
            "state": "confirmed",
        },
    ]
    ekaterinburg_tariffs = [
        {
            "name": "Economy Basic",
            "price": "15 900 ₽",
            "variant_id": "ekaterinburg-basic-v1",
            "checkout_ref": "ekaterinburg-basic",
            "details": ["ручная кладь", "багаж: нужно проверить", "отдельное оформление"],
            "state": "unknown",
        },
        {
            "name": "Economy Plus",
            "price": "20 870 ₽",
            "variant_id": "ekaterinburg-plus-v1",
            "checkout_ref": "ekaterinburg-plus",
            "details": ["багаж: 1 место", "изменение: уточнить на Туту", "отдельное оформление"],
            "state": "confirmed",
        },
    ]

    balance_units = [
        {
            "component_ref": "common-ist-lhr",
            "title": "Общее плечо · IST → LHR",
            "scope": "Один выбранный рейс для всех трёх участников",
            "handoff_note": "После выбора будет создана одна ссылка для трёх пассажиров.",
            "tariffs": common_tariffs,
        },
        {
            "component_ref": "moscow-ist",
            "title": "Фидер Ани · Москва → IST",
            "scope": "Отдельный билет до точки встречи",
            "handoff_note": "Это отдельное оформление, не единая бронь с общим плечом.",
            "tariffs": moscow_tariffs,
        },
        {
            "component_ref": "petersburg-ist",
            "title": "Фидер Ильи · Санкт-Петербург → IST",
            "scope": "Отдельный билет до точки встречи",
            "handoff_note": "Это отдельное оформление, не единая бронь с общим плечом.",
            "tariffs": petersburg_tariffs,
        },
        {
            "component_ref": "ekaterinburg-ist",
            "title": "Фидер Саши · Екатеринбург → IST",
            "scope": "Отдельный билет до точки встречи",
            "handoff_note": "Это отдельное оформление, не единая бронь с общим плечом.",
            "tariffs": ekaterinburg_tariffs,
        },
    ]

    return {
        "run_id": run_id,
        "query": query,
        "summary": (
            "Нашлись варианты встречи в IST: личные рейсы до Стамбула и общее плечо IST → LHR."
        ),
        "contract": {
            "title": "Договор группы",
            "route": "Москва · Санкт-Петербург · Екатеринбург → IST → Лондон (LHR)",
            "participants": [
                {"name": "Аня", "origin": "Москва · VKO"},
                {"name": "Илья", "origin": "Санкт-Петербург · LED"},
                {"name": "Саша", "origin": "Екатеринбург · SVX"},
            ],
            "hard_constraints": [
                {"label": "Точка встречи: именно IST", "state": "confirmed"},
                {"label": "Окно ожидания: 2–5 часов", "state": "confirmed"},
                {"label": "Общее последнее плечо для 3 человек", "state": "confirmed"},
            ],
            "soft_preferences": [
                "Багаж — выбрать только там, где условие подтверждено",
                "Избегать ночного ожидания в аэропорту",
            ],
        },
        "timeline": [
            {
                "person": "Аня · Москва",
                "arrival": "10:10 · IST",
                "wait": "3 ч 20 мин до общего рейса",
                "legs": [
                    _demo_timeline_leg("06:10–10:10", "VKO → IST", "VKO", "IST"),
                    _demo_timeline_leg("13:30–16:05", "IST → LHR · общий рейс", "IST", "LHR"),
                ],
            },
            {
                "person": "Илья · Санкт-Петербург",
                "arrival": "09:35 · IST",
                "wait": "3 ч 55 мин до общего рейса",
                "legs": [
                    _demo_timeline_leg("05:55–09:35", "LED → IST", "LED", "IST"),
                    _demo_timeline_leg("13:30–16:05", "IST → LHR · общий рейс", "IST", "LHR"),
                ],
            },
            {
                "person": "Саша · Екатеринбург",
                "arrival": "11:00 · IST",
                "wait": "2 ч 30 мин до общего рейса",
                "legs": [
                    _demo_timeline_leg("06:20–11:00", "SVX → IST", "SVX", "IST"),
                    _demo_timeline_leg("13:30–16:05", "IST → LHR · общий рейс", "IST", "LHR"),
                ],
            },
        ],
        "scenarios": [
            {
                "id": "price",
                "tone": "price",
                "title": "Дешевле",
                "subtitle": "Минимальная стоимость среди прошедших жёсткий договор",
                "state": "risk",
                "metrics": [
                    {"label": "Итого", "value": "от 148 600 ₽"},
                    {"label": "Макс. ожидание", "value": "4 ч 55 мин"},
                    {"label": "Общее плечо", "value": "IST → LHR"},
                ],
                "evidence": [
                    {
                        "state": "confirmed",
                        "title": "Точная точка встречи",
                        "body": "Все участники прибывают в IST, а не в другой аэропорт Стамбула.",
                        "source": "Tutu MCP · сегменты офферов",
                    },
                    {
                        "state": "risk",
                        "title": "Ночная нагрузка",
                        "body": "Два личных фидера стартуют до 06:00. Это не нарушение договора, но поездка будет тяжёлой.",
                        "source": "Расчёт из времён сегментов",
                    },
                    {
                        "state": "unknown",
                        "title": "Сквозной багаж не доказан",
                        "body": "Маршрут состоит из отдельных оформлений; отсутствие флага не доказывает сквозную регистрацию.",
                        "source": "Tutu MCP · условия тарифа",
                    },
                ],
                "booking_units": [],
            },
            {
                "id": "balance",
                "tone": "balance",
                "title": "Баланс",
                "subtitle": "Рекомендуемый сценарий: подтверждённый багаж на общем плече",
                "state": "confirmed",
                "metrics": [
                    {"label": "Итого", "value": "от 171 240 ₽"},
                    {"label": "Макс. ожидание", "value": "3 ч 55 мин"},
                    {"label": "Общее плечо", "value": "IST → LHR"},
                ],
                "evidence": [
                    {
                        "state": "confirmed",
                        "title": "Стыковки в договорном окне",
                        "body": "У каждого участника до общего вылета 2–5 часов ожидания в IST.",
                        "source": "GroupSync solver",
                    },
                    {
                        "state": "confirmed",
                        "title": "Один общий сегмент",
                        "body": "Все трое летят на одном точном плече IST → LHR после встречи.",
                        "source": "Tutu MCP · номер и время сегмента",
                    },
                    {
                        "state": "risk",
                        "title": "Это не единая бронь",
                        "body": "Фидеры оформляются отдельно от общего сегмента. Не выдаём это за защищённую пересадку.",
                        "source": "Структура checkout",
                    },
                ],
                "booking_units": balance_units,
            },
            {
                "id": "calm",
                "tone": "calm",
                "title": "Спокойнее",
                "subtitle": "Больше буфера и менее ранние личные выезды",
                "state": "confirmed",
                "metrics": [
                    {"label": "Итого", "value": "от 196 800 ₽"},
                    {"label": "Макс. ожидание", "value": "3 ч 15 мин"},
                    {"label": "Общее плечо", "value": "IST → LHR"},
                ],
                "evidence": [
                    {
                        "state": "confirmed",
                        "title": "Более ровная группа",
                        "body": "Разброс прибытия в IST меньше, поэтому никто не ждёт остальных почти весь день.",
                        "source": "GroupSync solver",
                    },
                    {
                        "state": "confirmed",
                        "title": "Точный аэропорт сохранён",
                        "body": "Не требуется наземный переезд IST ↔ SAW.",
                        "source": "Tutu MCP · точки сегментов",
                    },
                    {
                        "state": "unknown",
                        "title": "Условия личных тарифов",
                        "body": "Багаж и обмен каждого фидера нужно подтвердить при выборе конкретного тарифа.",
                        "source": "Tutu MCP · get_offer_details",
                    },
                ],
                "booking_units": [],
            },
        ],
        "rejection_summary": (
            "Скрыто 9 комбинаций: 5 не попали в окно 2–5 часов, 3 пришли в SAW вместо IST, "
            "1 не имела подтверждённого общего последнего плеча."
        ),
        "trace": [
            {"tool": "search_avia", "summary": "Найдены личные фидеры и общее плечо через точный IST."},
            {"tool": "solve_group_rendezvous", "summary": "Проверено окно ожидания и совпадение общего сегмента."},
            {"tool": "inspect_offer_risks", "summary": "Отделены подтверждённые факты, риски и неизвестные поля."},
        ],
    }


app = create_app()

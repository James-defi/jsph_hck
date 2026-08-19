"""Run one bounded live GroupSync diagnostic without leaking secrets.

The log is deliberately operational rather than forensic: it contains timing,
provider choice, tool names, safe tool arguments, offer counts and failures.
It never stores the OpenRouter key, hidden model reasoning, full MCP offers,
``checkout_ref`` objects or complete checkout URLs.

Example from the repository root::

    python agent/scripts/run_live_diagnostic.py --timeout-seconds 900 --create-handoff
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import traceback
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agent.app.application import build_live_service
from agent.app.config import get_settings
from agent.app.openrouter import OpenRouterClient
from agent.app.tutu_mcp import StreamableHttpMcpClient, TutuMcpGateway


DEFAULT_PROMPT = (
    "Нас трое взрослых: Анна вылетает из аэропорта Внуково, Илья — из "
    "аэропорта Пулково, Саша — из аэропорта Кольцово. 10 сентября 2026 года "
    "хотим встретиться в Новом аэропорту Стамбула, а потом все вместе улететь "
    "в аэропорт Хитроу. Каждый должен прибыть за 4–8 часов до общего рейса. "
    "Багаж не нужен. Найди до трёх проверяемых сценариев, покажи "
    "подтверждённые факты и риски."
)

_SECRET_KEYS = frozenset({"checkout_ref", "checkout_url", "url", "authorization", "api_key", "reasoning", "reasoning_details"})
_SAFE_ARGUMENT_KEYS = frozenset(
    {
        "origin",
        "destination",
        "departure_date",
        "return_departure_at",
        "adults",
        "children",
        "infants",
        "view",
        "mode",
        "common_mode",
        "feeder_mode",
    }
)


class JsonlLog:
    """Flush each event so a timeout still leaves a useful diagnostic trail."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("a", encoding="utf-8")

    def event(self, name: str, **data: Any) -> None:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": name,
            **_redact(data),
        }
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalised = str(key).lower()
            if normalised in _SECRET_KEYS:
                if normalised == "url" and isinstance(item, str):
                    result[str(key)] = {"host": urlparse(item).netloc, "redacted": True}
                else:
                    result[str(key)] = "[redacted]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_redact(item) for item in value]
    return value


def _safe_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        return {"arguments_type": type(arguments).__name__}
    result = {key: arguments[key] for key in _SAFE_ARGUMENT_KEYS if key in arguments}
    if "checkout_ref" in arguments:
        result["checkout_ref_present"] = True
    contract = arguments.get("contract")
    if isinstance(contract, Mapping):
        raw_participants = contract.get("participants")
        participants: list[dict[str, Any]] = []
        if isinstance(raw_participants, Sequence) and not isinstance(raw_participants, (str, bytes)):
            for item in raw_participants:
                if not isinstance(item, Mapping):
                    continue
                participant: dict[str, Any] = {}
                origin = item.get("origin") or item.get("origin_code")
                if isinstance(origin, str) and origin.strip():
                    participant["origin"] = origin.strip().upper()
                adults = item.get("adults")
                if isinstance(adults, int) and not isinstance(adults, bool):
                    participant["adults"] = adults
                participants.append(participant)
        result["contract"] = {
            "participants": participants,
            "hub_code": contract.get("hub_code"),
            "destination_code": contract.get("destination_code"),
            "min_wait_minutes": contract.get("min_wait_minutes"),
            "max_wait_minutes": contract.get("max_wait_minutes"),
            "required_checked_baggage_pieces": contract.get("required_checked_baggage_pieces"),
            "strict_baggage": contract.get("strict_baggage"),
        }
    if not result:
        result["keys"] = sorted(str(key) for key in arguments)[:20]
    return result


def _offer_count(value: Any) -> int:
    if isinstance(value, Mapping):
        for key in ("offers", "results", "items"):
            candidate = value.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                return len(candidate)
        nested = value.get("data")
        if isinstance(nested, Mapping):
            return _offer_count(nested)
    return 0


def _offers(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in ("offers", "results", "items"):
            candidate = value.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                return [item for item in candidate if isinstance(item, Mapping)]
        nested = value.get("data")
        if isinstance(nested, Mapping):
            return _offers(nested)
    return []


def _segment_count(offer: Mapping[str, Any]) -> int:
    direct = offer.get("segments")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        return sum(1 for item in direct if isinstance(item, Mapping))
    legs = offer.get("legs")
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        return 0
    total = 0
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        segments = leg.get("segments")
        if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes)):
            total += sum(1 for item in segments if isinstance(item, Mapping))
    return total


def _mcp_result_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"result_type": type(value).__name__}
    found_offers = _offers(value)
    counts: dict[str, int] = {}
    for offer in found_offers:
        count = _segment_count(offer)
        counts[str(count)] = counts.get(str(count), 0) + 1
    return {
        "top_level_keys": sorted(str(key) for key in value)[:20],
        "offers": len(found_offers),
        "offer_segment_counts": counts,
        "has_error": bool(value.get("error")),
    }


class LoggedTutuGateway:
    """Proxy that records one redacted event for every synchronous MCP call."""

    def __init__(self, inner: TutuMcpGateway, log: JsonlLog) -> None:
        self._inner = inner
        self._log = log

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            arguments = args[0] if args and isinstance(args[0], Mapping) else kwargs
            started = monotonic()
            self._log.event("tutu_call_started", tool=name, arguments=_safe_arguments(arguments))
            try:
                result = target(*args, **kwargs)
            except Exception as exc:
                self._log.event(
                    "tutu_call_failed",
                    tool=name,
                    duration_ms=round((monotonic() - started) * 1000),
                    error_type=type(exc).__name__,
                    message=str(exc)[:500],
                )
                raise
            self._log.event(
                "tutu_call_finished",
                tool=name,
                duration_ms=round((monotonic() - started) * 1000),
                result=_mcp_result_summary(result),
            )
            return result

        return wrapped


class LoggedOpenRouterClient(OpenRouterClient):
    """Expose only status/provider metadata for the live diagnostic."""

    def __init__(self, *, diagnostics: JsonlLog, **kwargs: Any) -> None:
        self._diagnostics = diagnostics
        super().__init__(**kwargs)

    @classmethod
    def from_live_settings(cls, settings: Any, diagnostics: JsonlLog) -> "LoggedOpenRouterClient":
        return cls(
            diagnostics=diagnostics,
            api_key=settings.require_openrouter_api_key(),
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            provider=None,
            providers=settings.openrouter_provider_order,
            allow_fallbacks=settings.openrouter_allow_fallbacks,
            require_parameters=settings.openrouter_require_parameters,
            parallel_tool_calls=settings.openrouter_parallel_tool_calls,
            reasoning_enabled=settings.openrouter_reasoning_enabled,
            timeout_seconds=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
            retry_backoff_seconds=settings.openrouter_retry_backoff_seconds,
            max_retry_delay_seconds=settings.openrouter_max_retry_delay_seconds,
        )

    async def _post_with_retries(self, payload: dict[str, Any]):  # type: ignore[override]
        self._diagnostics.event(
            "openrouter_request_started",
            model=self.model,
            provider_order=list(self.providers),
            provider_only=list(self.providers),
            allow_fallbacks=self.allow_fallbacks,
            require_parameters=self.require_parameters,
            tool_count=len(payload.get("tools", [])),
        )
        started = monotonic()
        try:
            response = await super()._post_with_retries(payload)
        except Exception as exc:
            self._diagnostics.event(
                "openrouter_request_failed",
                duration_ms=round((monotonic() - started) * 1000),
                error_type=type(exc).__name__,
                message=str(exc)[:500],
            )
            raise

        provider: Any = None
        error: Any = None
        tool_calls: list[dict[str, Any]] = []
        try:
            body = response.json()
            if isinstance(body, Mapping):
                provider = body.get("provider") or body.get("metadata")
                error = body.get("error")
                if response.status_code < 400:
                    turn = OpenRouterClient.extract_assistant_turn(body)
                    tool_calls = [
                        {"name": call.name, "arguments": _safe_arguments(call.arguments)}
                        for call in turn.tool_calls
                    ]
        except Exception:
            pass
        self._diagnostics.event(
            "openrouter_response_received",
            duration_ms=round((monotonic() - started) * 1000),
            http_status=response.status_code,
            provider=provider,
            error=error,
            tool_calls=tool_calls,
        )
        return response


def _agent_trace(result: Any) -> list[dict[str, Any]]:
    trace = result.get("trace") if isinstance(result, Mapping) else None
    if isinstance(trace, list):
        return [_redact(item) for item in trace if isinstance(item, Mapping)]
    events = getattr(trace, "events", [])
    summary: list[dict[str, Any]] = []
    for event in events:
        item: dict[str, Any] = {"step": event.step, "kind": event.kind, "detail": event.detail}
        if event.tool_calls:
            item["tool_calls"] = [call.tool_name for call in event.tool_calls]
        if event.tool_execution is not None:
            item["tool"] = event.tool_execution.tool_name
            item["arguments"] = _safe_arguments(event.tool_execution.arguments)
            item["tool_success"] = event.tool_execution.success
            item["tool_error"] = event.tool_execution.error
            item["duration_ms"] = event.tool_execution.duration_ms
        summary.append(item)
    return summary


def _write_summary(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(_redact(payload), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def run(args: argparse.Namespace, log: JsonlLog, summary_path: Path) -> int:
    settings = get_settings()
    started = monotonic()
    log.event(
        "run_started",
        prompt=args.prompt,
        timeout_seconds=args.timeout_seconds,
        create_handoff=args.create_handoff,
        model=settings.openrouter_model,
        provider_order=settings.openrouter_provider_order,
        allow_fallbacks=settings.openrouter_allow_fallbacks,
    )
    _write_summary(summary_path, {"status": "running", "log_file": str(log.path)})

    gateway = LoggedTutuGateway(
        TutuMcpGateway(StreamableHttpMcpClient(settings.tutu_mcp_url)),
        log,
    )
    service = build_live_service(
        settings=settings,
        tutu=gateway,  # type: ignore[arg-type]
        llm_factory=lambda: LoggedOpenRouterClient.from_live_settings(settings, log),
    )

    try:
        async with asyncio.timeout(args.timeout_seconds):
            result = await service.run(args.prompt)
        stored = service.run_store.get(result["run_id"])
        outcome: dict[str, Any] = {
            "status": str(result.get("status")),
            "summary": result.get("summary"),
            "scenarios": len(result.get("scenarios") or []),
            "rejection_summary": result.get("rejection_summary"),
            "agent_trace": _agent_trace(result),
            "checkout_components": len(stored.components),
        }
        log.event("agent_run_finished", **outcome)

        if args.create_handoff and stored.components:
            component_ref, component = next(iter(stored.components.items()))
            variant_id = next(iter(component.variants))
            handoff = await service.create_checkout_link(result["run_id"], component_ref, variant_id)
            outcome["handoff"] = {
                "attempted": True,
                "created": bool(handoff.get("url")),
                "host": urlparse(str(handoff.get("url") or "")).netloc,
                "kind": handoff.get("handoff_kind"),
            }
            log.event("checkout_handoff_finished", **outcome["handoff"])
        elif args.create_handoff:
            log.event("checkout_handoff_skipped", reason="no_selectable_component")

        outcome.update(
            {
                "diagnostic_status": "completed",
                "elapsed_seconds": round(monotonic() - started, 3),
                "log_file": str(log.path),
            }
        )
        _write_summary(summary_path, outcome)
        return 0
    except TimeoutError:
        outcome = {
            "diagnostic_status": "timed_out",
            "timeout_seconds": args.timeout_seconds,
            "elapsed_seconds": round(monotonic() - started, 3),
            "log_file": str(log.path),
        }
        log.event("run_timed_out", **outcome)
        _write_summary(summary_path, outcome)
        return 2
    except Exception as exc:
        outcome = {
            "diagnostic_status": "failed",
            "error_type": type(exc).__name__,
            "message": str(exc)[:1000],
            "traceback": "".join(traceback.format_exception(exc))[-4000:],
            "elapsed_seconds": round(monotonic() - started, 3),
            "log_file": str(log.path),
        }
        log.event("run_failed", **outcome)
        _write_summary(summary_path, outcome)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=REPOSITORY_ROOT / "agent" / "data" / "diagnostics")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("live-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--create-handoff", action="store_true")
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt", default=None)
    # ``Start-Process`` on Windows may pass non-ASCII command-line arguments
    # through a legacy code page.  An explicit UTF-8 base64 form keeps live
    # diagnostics reproducible while leaving the readable --prompt option for
    # interactive shells.
    prompt_group.add_argument("--prompt-base64", default=None)
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 900:
        parser.error("--timeout-seconds must be between 1 and 900")
    if args.prompt_base64:
        try:
            args.prompt = base64.b64decode(args.prompt_base64, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            parser.error(f"--prompt-base64 must be valid UTF-8 base64: {exc}")
    elif args.prompt is None:
        args.prompt = DEFAULT_PROMPT
    return args


def main() -> int:
    args = parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"{args.run_id}.jsonl"
    summary_path = args.log_dir / f"{args.run_id}.summary.json"
    log = JsonlLog(log_path)
    try:
        return asyncio.run(run(args, log, summary_path))
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic SafetyGate / RecommendationPolicy between solver and UI.

The solver remains the hard contract filter (hub, meeting window, signature).
This layer decides whether a surviving combination may be recommended and
whether a checkout inventory/CTA is allowed.  Verdicts are independent of LLM
prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .geo import meeting_point_matches
from .solver import GroupTripContract, _crosses_night, parse_datetime


JSON = dict[str, Any]
SafetyStatus = Literal["blocked", "needs_verification", "caution", "recommended"]

AIR_MODES = frozenset({"avia", "air", "flight", "plane", "airline", "самолёт", "самолет", "авиа"})
RAIL_MODES = frozenset({"rail", "train", "etrain", "поезд", "жд", "railway"})
BUS_MODES = frozenset({"bus", "coach", "автобус"})

FLOOR_AIR_ANY = 240
FLOOR_RAIL_SAME_STATION = 30
FLOOR_BUS_SAME_STOP = 45
DEFAULT_SELF_TRANSFER_FLOOR = 240

_STATUS_RANK = {
    "recommended": 0,
    "caution": 1,
    "needs_verification": 2,
    "blocked": 3,
}


def _as_mapping(value: Any) -> JSON:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def _label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _same_meeting_point(
    left_code: str | None,
    left_label: str | None,
    right_code: str | None,
    right_label: str | None,
    *,
    mode: str,
) -> bool:
    """True when two offer endpoints describe one transfer point."""

    for requested in (left_code, left_label):
        if not requested:
            continue
        if meeting_point_matches(requested, code=right_code, label=right_label, mode=mode):
            return True
    return False


def _transport_family(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"unknown", "none"}:
        return "unknown"
    if text in AIR_MODES:
        return "air"
    if text in RAIL_MODES:
        return "rail"
    if text in BUS_MODES:
        return "bus"
    return "unknown"


def _offer_mode(offer: Mapping[str, Any]) -> str:
    return _transport_family(
        offer.get("mode") or offer.get("transport") or offer.get("transport_type") or offer.get("type")
    )


def _segment_mode(segment: Mapping[str, Any], *, fallback: str = "unknown") -> str:
    family = _transport_family(segment.get("mode") or segment.get("transport"))
    return family if family != "unknown" else fallback


def _segments(offer: Mapping[str, Any]) -> list[JSON]:
    direct = _as_list(offer.get("segments"))
    if direct:
        return [_as_mapping(item) for item in direct]
    legs = _as_list(offer.get("legs"))
    segments: list[JSON] = []
    for leg in legs:
        mapping = _as_mapping(leg)
        nested = _as_list(mapping.get("segments"))
        if nested:
            segments.extend(_as_mapping(item) for item in nested)
        elif mapping:
            segments.append(mapping)
    return segments


def _minutes_between(start: Any, end: Any) -> int | None:
    first = parse_datetime(start)
    second = parse_datetime(end)
    if first is None or second is None:
        return None
    if (first.tzinfo is None) != (second.tzinfo is None):
        return None
    return int((second - first).total_seconds() // 60)


def _has_exact_tariff_ref(offer: Mapping[str, Any]) -> bool:
    ref = offer.get("checkout_ref")
    return isinstance(ref, Mapping) and bool(ref)


def _self_transfer_floor(from_mode: str, to_mode: str, *, same_point: bool | None) -> int | None:
    """Return required minutes, or None when the pair cannot be timed safely.

    ``None`` means the caller should emit ``needs_verification`` (unknown
    transfer point) rather than a numeric floor.
    """

    left = from_mode if from_mode != "unknown" else "air"
    right = to_mode if to_mode != "unknown" else "air"
    if left == "unknown" or right == "unknown":
        left, right = "air", "air"
    if left == "air" or right == "air":
        return FLOOR_AIR_ANY
    if left == "rail" and right == "rail":
        if same_point is True:
            return FLOOR_RAIL_SAME_STATION
        return None
    if left == "bus" and right == "bus":
        if same_point is True:
            return FLOOR_BUS_SAME_STOP
        return None
    return DEFAULT_SELF_TRANSFER_FLOOR


def _protected_single_ticket_mct(
    feeder_offer: Mapping[str, Any],
    common_offer: Mapping[str, Any],
) -> int | None:
    """Use MCP MCT only for a confirmed protected single ticket.

    Independently purchased feeder + common legs are never this case.
    Same airline is not treated as proof.  Field names are read if present
    and never invented into the public contract.
    """

    feeder_id = str(feeder_offer.get("id") or "")
    common_id = str(common_offer.get("id") or "")
    if not feeder_id or feeder_id != common_id:
        return None
    if feeder_offer.get("is_multi_pnr") is not False:
        return None
    if common_offer.get("is_multi_pnr") is not False:
        return None
    for source in (feeder_offer, common_offer):
        for key in ("min_connection_minutes", "minimum_connection_minutes", "mct_minutes"):
            value = source.get(key)
            try:
                if value is None or isinstance(value, bool):
                    continue
                minutes = int(value)
            except (TypeError, ValueError):
                continue
            if minutes >= 0:
                return minutes
    return None


def _buffer_reason(actual: int, required: int) -> str:
    return (
        f"Не рекомендуем: на самостоятельную пересадку есть {actual} минут, "
        f"а нужен запас не меньше {required} минут. Второй билет не защищён, "
        "поэтому ссылку на оформление мы не покажем."
    )


@dataclass(frozen=True)
class SafetyVerdict:
    status: SafetyStatus
    reasons: tuple[str, ...] = ()

    @property
    def allows_checkout(self) -> bool:
        return self.status == "recommended"

    @property
    def reason(self) -> str:
        return " ".join(self.reasons)


class RecommendationPolicy:
    """Classify one solver scenario for purchase CTA and checkout inventory."""

    def evaluate(
        self,
        contract: GroupTripContract | Mapping[str, Any],
        scenario: Mapping[str, Any],
    ) -> SafetyVerdict:
        typed = contract if isinstance(contract, GroupTripContract) else GroupTripContract.from_mapping(contract)
        blocked: list[str] = []
        verify: list[str] = []
        caution: list[str] = []

        common = _as_mapping(scenario.get("common_offer"))
        signature = _as_mapping(scenario.get("common_service_signature"))
        feeders = [_as_mapping(item) for item in _as_list(scenario.get("feeders"))]
        common_segments = _segments(common)
        common_first = _as_mapping(common_segments[0]) if common_segments else {}
        common_last = _as_mapping(common_segments[-1]) if common_segments else {}
        common_origin = _code(signature.get("origin_code") or common_first.get("origin_code"))
        # A single ground ticket may legally include a connection, so the trip
        # ends at the *last* segment even when the shared service signature
        # describes only the first one.
        common_destination = _code(common_last.get("destination_code") or signature.get("destination_code"))
        common_origin_label = _label(common_first.get("origin_name")) or _label(common.get("origin_city"))
        common_destination_label = _label(common_last.get("destination_name")) or _label(
            common.get("destination_city")
        )
        common_departure = signature.get("departure_at") or common_first.get("departure_at")
        common_mode = _segment_mode(signature, fallback=_offer_mode(common))

        if common_origin and not meeting_point_matches(
            typed.hub_code, code=common_origin, label=common_origin_label, mode=common_mode
        ):
            blocked.append(
                f"Не рекомендуем: общий сегмент стартует в {common_origin}, а договор требует хаб {typed.hub_code}."
            )
        if common_destination and not meeting_point_matches(
            typed.destination_code, code=common_destination, label=common_destination_label, mode=common_mode
        ):
            blocked.append(
                f"Не рекомендуем: общий сегмент едет в {common_destination}, а договор требует {typed.destination_code}."
            )
        if typed.expected_common_signature:
            for key, wanted in typed.expected_common_signature.items():
                if wanted is None or signature.get(key) is None:
                    continue
                left = _code(wanted) if key in {"carrier", "service_number", "origin_code", "destination_code", "mode"} else wanted
                right = _code(signature.get(key)) if key in {"carrier", "service_number", "origin_code", "destination_code", "mode"} else signature.get(key)
                if left != right:
                    blocked.append("Не рекомендуем: точный общий сегмент не совпадает с договором.")
                    break

        if not feeders:
            verify.append("Нельзя подтвердить эту пересадку: в данных нет фидера или точки перехода.")

        exact_refs = _has_exact_tariff_ref(common) if common else False
        for feeder in feeders:
            offer = _as_mapping(feeder.get("offer"))
            segments = _segments(offer)
            last = _as_mapping(segments[-1]) if segments else {}
            arrival_point = _code(last.get("destination_code"))
            arrival_label = _label(last.get("destination_name")) or _label(offer.get("destination_city"))
            arrival_at = last.get("arrival_at")
            feeder_mode = _segment_mode(last, fallback=_offer_mode(offer))
            wait = feeder.get("wait_minutes")
            try:
                wait_minutes = int(wait) if wait is not None and not isinstance(wait, bool) else None
            except (TypeError, ValueError):
                wait_minutes = None
            if wait_minutes is None:
                wait_minutes = _minutes_between(arrival_at, common_departure)

            if arrival_point and not meeting_point_matches(
                typed.hub_code, code=arrival_point, label=arrival_label, mode=feeder_mode
            ):
                blocked.append(
                    f"Не рекомендуем: участник {feeder.get('participant_id') or ''} прибывает в {arrival_point}, "
                    f"а встреча нужна в {typed.hub_code}.".replace("  ", " ")
                )
            transfer_at_one_point = _same_meeting_point(
                arrival_point,
                arrival_label,
                common_origin,
                common_origin_label,
                mode=feeder_mode,
            )
            if not arrival_point or not common_origin:
                verify.append("Нельзя подтвердить эту пересадку: в данных нет времени или точки перехода.")
            elif not transfer_at_one_point:
                blocked.append(
                    f"Не рекомендуем: прибытие в {arrival_point}, а общий сегмент уходит из {common_origin}. "
                    "Переход между разными аэропортами или вокзалами без подтверждённой пересадки не оформляем."
                )

            if wait_minutes is None:
                verify.append("Нельзя подтвердить эту пересадку: в данных нет времени или точки перехода.")
            elif wait_minutes < 0:
                blocked.append(
                    f"Не рекомендуем: общее плечо уходит раньше прибытия ({wait_minutes} минут относительно стыковки)."
                )
            elif wait_minutes > typed.max_wait_minutes:
                blocked.append(
                    f"Не рекомендуем: ожидание {wait_minutes} минут больше максимума из договора "
                    f"({typed.max_wait_minutes} минут)."
                )
            else:
                same_point = bool(arrival_point and common_origin and transfer_at_one_point)
                protected_mct = _protected_single_ticket_mct(offer, common)
                required = (
                    protected_mct
                    if protected_mct is not None
                    else _self_transfer_floor(feeder_mode, common_mode, same_point=same_point if same_point else None)
                )
                if required is None:
                    verify.append(
                        "Нельзя подтвердить эту пересадку: разные станции или остановки без подтверждённой точки перехода."
                    )
                elif wait_minutes < required:
                    blocked.append(_buffer_reason(wait_minutes, required))

            arrival_dt = parse_datetime(arrival_at)
            depart_dt = parse_datetime(common_departure)
            night = _crosses_night(arrival_dt, depart_dt)
            if night is True:
                caution.append("Ночная стыковка: ожидание затрагивает часы 00:00–06:00, основной CTA покупки не показываем.")
            if offer.get("is_multi_pnr") is True or common.get("is_multi_pnr") is True:
                caution.append("Это отдельные билеты: стыковка не защищена, поэтому основной CTA покупки не показываем.")
            if arrival_point and common_origin and not transfer_at_one_point:
                caution.append("Смена аэропорта или станции на стыковке: основной CTA покупки не показываем.")

            baggage = _as_mapping(offer.get("baggage"))
            common_baggage = _as_mapping(common.get("baggage"))
            if typed.strict_baggage and typed.required_checked_baggage_pieces:
                confirmed = baggage.get("checked_pieces")
                try:
                    pieces = int(confirmed) if confirmed is not None else None
                except (TypeError, ValueError):
                    pieces = None
                if pieces is None or pieces < typed.required_checked_baggage_pieces:
                    blocked.append(
                        "Не рекомендуем: обязательный багаж не подтверждён выбранным тарифом."
                    )
            if typed.required_checked_baggage_pieces and (
                baggage.get("through_checked") is False or common_baggage.get("through_checked") is False
            ):
                caution.append("Багаж нужно сдавать самостоятельно: основной CTA покупки не показываем.")

            if not _has_exact_tariff_ref(offer):
                exact_refs = False
            elif not exact_refs:
                exact_refs = False

        if common and not _has_exact_tariff_ref(common):
            exact_refs = False

        if blocked:
            # Prefer the concrete time-buffer sentence when it is the cause.
            ordered = sorted(blocked, key=lambda text: (0 if "нужен запас не меньше" in text else 1, text))
            unique = tuple(dict.fromkeys(ordered))
            return SafetyVerdict("blocked", unique)
        if verify:
            unique = tuple(dict.fromkeys(verify))
            return SafetyVerdict("needs_verification", unique)
        if caution:
            unique = tuple(dict.fromkeys(caution))
            return SafetyVerdict("caution", unique)
        if not exact_refs:
            return SafetyVerdict(
                "needs_verification",
                ("Нельзя подтвердить тариф: нет точного идентификатора оформления, поэтому рекомендацию и ссылку не показываем.",),
            )
        return SafetyVerdict("recommended", ())

    def evaluate_offer(
        self,
        offer: Mapping[str, Any],
        *,
        max_wait_minutes: int,
        min_wait_minutes: int = 0,
    ) -> SafetyVerdict:
        """Classify one A→B ticket by consecutive internal connections.

        A simple direct fare does not need a group hub/feeder contract.  The
        same numeric floors as group self-transfer still apply between
        consecutive segments of this offer.
        """

        blocked: list[str] = []
        verify: list[str] = []
        caution: list[str] = []
        body = _as_mapping(offer)
        segments = _segments(body)
        if not segments:
            return SafetyVerdict(
                "needs_verification",
                ("Нельзя подтвердить маршрут: в данных нет сегментов.",),
            )

        offer_mode = _offer_mode(body)
        for previous, following in zip(segments, segments[1:]):
            left = _as_mapping(previous)
            right = _as_mapping(following)
            arrival_point = _code(left.get("destination_code"))
            depart_point = _code(right.get("origin_code"))
            arrival_at = left.get("arrival_at")
            depart_at = right.get("departure_at")
            wait_minutes = _minutes_between(arrival_at, depart_at)
            from_mode = _segment_mode(left, fallback=offer_mode)
            to_mode = _segment_mode(right, fallback=offer_mode)

            if not arrival_point or not depart_point:
                verify.append("Нельзя подтвердить эту пересадку: в данных нет времени или точки перехода.")
            elif arrival_point != depart_point:
                blocked.append(
                    f"Не рекомендуем: прибытие в {arrival_point}, а следующий сегмент уходит из {depart_point}. "
                    "Переход между разными аэропортами или вокзалами без подтверждённой пересадки не оформляем."
                )
                caution.append("Смена аэропорта или станции на стыковке: основной CTA покупки не показываем.")

            if wait_minutes is None:
                verify.append("Нельзя подтвердить эту пересадку: в данных нет времени или точки перехода.")
            elif wait_minutes < 0:
                blocked.append(
                    f"Не рекомендуем: следующее плечо уходит раньше прибытия ({wait_minutes} минут относительно стыковки)."
                )
            elif wait_minutes > max_wait_minutes:
                blocked.append(
                    f"Не рекомендуем: ожидание {wait_minutes} минут больше максимума из договора "
                    f"({max_wait_minutes} минут)."
                )
            elif wait_minutes < min_wait_minutes:
                blocked.append(
                    f"Не рекомендуем: ожидание {wait_minutes} минут меньше минимума из договора "
                    f"({min_wait_minutes} минут)."
                )
            else:
                same_point = bool(arrival_point and depart_point and arrival_point == depart_point)
                required = _self_transfer_floor(
                    from_mode,
                    to_mode,
                    same_point=same_point if same_point else None,
                )
                if required is None:
                    verify.append(
                        "Нельзя подтвердить эту пересадку: разные станции или остановки без подтверждённой точки перехода."
                    )
                elif wait_minutes < required:
                    blocked.append(_buffer_reason(wait_minutes, required))

            night = _crosses_night(parse_datetime(arrival_at), parse_datetime(depart_at))
            if night is True:
                caution.append("Ночная стыковка: ожидание затрагивает часы 00:00–06:00, основной CTA покупки не показываем.")

        if body.get("is_multi_pnr") is True or body.get("is_self_transfer") is True:
            caution.append("Это отдельные билеты: стыковка не защищена, поэтому основной CTA покупки не показываем.")

        if blocked:
            ordered = sorted(blocked, key=lambda text: (0 if "нужен запас не меньше" in text else 1, text))
            unique = tuple(dict.fromkeys(ordered))
            return SafetyVerdict("blocked", unique)
        if verify:
            unique = tuple(dict.fromkeys(verify))
            return SafetyVerdict("needs_verification", unique)
        if caution:
            unique = tuple(dict.fromkeys(caution))
            return SafetyVerdict("caution", unique)
        if not _has_exact_tariff_ref(body):
            return SafetyVerdict(
                "needs_verification",
                ("Нельзя подтвердить тариф: нет точного идентификатора оформления, поэтому рекомендацию и ссылку не показываем.",),
            )
        return SafetyVerdict("recommended", ())


class SafetyGate:
    """Named gate used between solver output and presentation, and at checkout."""

    def __init__(self, policy: RecommendationPolicy | None = None) -> None:
        self.policy = policy or RecommendationPolicy()

    def evaluate_scenario(
        self,
        contract: GroupTripContract | Mapping[str, Any],
        scenario: Mapping[str, Any],
    ) -> SafetyVerdict:
        return self.policy.evaluate(contract, scenario)

    def evaluate_offer(
        self,
        offer: Mapping[str, Any],
        *,
        max_wait_minutes: int,
        min_wait_minutes: int = 0,
    ) -> SafetyVerdict:
        return self.policy.evaluate_offer(
            offer,
            max_wait_minutes=max_wait_minutes,
            min_wait_minutes=min_wait_minutes,
        )

    def require_recommended(
        self,
        contract: GroupTripContract | Mapping[str, Any] | None,
        scenario: Mapping[str, Any] | None,
        *,
        stored_verdict: str | None = None,
        offer: Mapping[str, Any] | None = None,
        max_wait_minutes: int = 24 * 60,
        min_wait_minutes: int = 0,
    ) -> None:
        """Raise ValueError unless this scenario may create a Tutu handoff."""

        if stored_verdict is not None and stored_verdict != "recommended":
            raise ValueError("Этот вариант нельзя оформить: он не прошёл проверку безопасности.")
        if offer is not None:
            verdict = self.evaluate_offer(
                offer,
                max_wait_minutes=max_wait_minutes,
                min_wait_minutes=min_wait_minutes,
            )
            if not verdict.allows_checkout:
                raise ValueError("Этот вариант нельзя оформить: он не прошёл проверку безопасности.")
            return
        if scenario is None:
            if stored_verdict == "recommended":
                return
            if stored_verdict is None:
                return
            raise ValueError("Этот вариант нельзя оформить: он не прошёл проверку безопасности.")
        if contract is None:
            if stored_verdict == "recommended":
                return
            raise ValueError("Этот вариант нельзя оформить: он не прошёл проверку безопасности.")
        verdict = self.evaluate_scenario(contract, scenario)
        if not verdict.allows_checkout:
            raise ValueError("Этот вариант нельзя оформить: он не прошёл проверку безопасности.")


def apply_safety_to_presentation(presentation: Mapping[str, Any]) -> JSON:
    """Annotate solver-shaped presentations and strip CTA unless recommended.

    Live cards produced by ``build_group_presentation`` already carry
    ``safety_verdict``.  Solver-shaped service results still have ``hub_code``
    on the contract and can be classified here.
    """

    result = dict(presentation)
    scenarios = result.get("scenarios")
    if not isinstance(scenarios, list):
        return result
    contract_raw = result.get("contract")
    typed: GroupTripContract | None = None
    if isinstance(contract_raw, Mapping) and contract_raw.get("hub_code"):
        try:
            typed = GroupTripContract.from_mapping(contract_raw)
        except ValueError:
            typed = None
    policy = RecommendationPolicy()
    annotated: list[Any] = []
    recommended = 0
    for item in scenarios:
        if not isinstance(item, Mapping):
            annotated.append(item)
            continue
        scenario = dict(item)
        verdict_status = scenario.get("safety_verdict")
        if verdict_status not in _STATUS_RANK and typed is not None and (
            "feeders" in scenario or "common_offer" in scenario
        ):
            verdict = policy.evaluate(typed, scenario)
            scenario["safety_verdict"] = verdict.status
            if verdict.reason:
                scenario["safety_reason"] = verdict.reason
            verdict_status = verdict.status
        if verdict_status and verdict_status != "recommended":
            scenario["booking_units"] = []
        if verdict_status == "recommended":
            recommended += 1
        annotated.append(scenario)
    result["scenarios"] = annotated
    if annotated and recommended == 0 and any(
        isinstance(item, Mapping) and item.get("safety_verdict") for item in annotated
    ):
        result.setdefault(
            "summary",
            "Нашлись комбинации по условиям встречи, но ни одну нельзя рекомендовать к оформлению.",
        )
    return result


def scenario_allows_checkout(scenario: Mapping[str, Any] | None) -> bool:
    if not isinstance(scenario, Mapping):
        return False
    return scenario.get("safety_verdict") == "recommended"

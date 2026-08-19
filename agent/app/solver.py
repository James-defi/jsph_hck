"""Deterministic GroupSync routing and purchase-risk checks.

LLM reasoning decides *which* searches to make.  This module decides the
facts that must not be hallucinated: exact hub matching, connection windows,
shared-service signatures, and what the returned offer actually confirms.

Public functions accept plain mappings and return JSON-compatible mappings so
they can sit between MCP data, a Pydantic boundary, and a web UI without a
hidden dependency on any of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .geo import meeting_point_matches


JSON = dict[str, Any]
STATUS_PASS = "pass"
STATUS_RISK = "risk"
STATUS_UNKNOWN = "unknown"
VALID_STATUSES = {STATUS_PASS, STATUS_RISK, STATUS_UNKNOWN}


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _code(value: Any) -> str | None:
    """Normalize an airport/station code without guessing a city-to-code map."""

    text = _clean_text(value)
    if not text:
        return None
    return text.upper()


def _first(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _point_code(value: Any) -> str | None:
    if isinstance(value, Mapping):
        nested = _first(
            value,
            "code",
            "iata",
            "iata_code",
            "station_code",
            "airport_code",
            "id",
        )
        if nested is None and isinstance(value.get("location"), Mapping):
            nested = _point_code(value["location"])
        return _code(nested)
    text = _clean_text(value)
    if not text:
        return None
    # Tutu aviation segments expose a self-describing endpoint, for example
    # ``Москва — Внуково (VKO), терм. A``.  The parenthesised IATA is supplied
    # by the current MCP response; extracting it is not a city-to-airport
    # guess.  A plain input is kept as-is for other transport domains.
    match = re.search(r"\(([A-Za-z0-9]{3,8})\)", text)
    if match:
        return match.group(1).upper()
    # The same playbook documents a compact fallback ``City, IATA`` when an
    # upstream omits the airport name.  Only accept a trailing ASCII code;
    # arbitrary city text remains untouched rather than being guessed.
    trailing_code = re.search(r",\s*([A-Za-z0-9]{3,8})\s*$", text)
    if trailing_code:
        return trailing_code.group(1).upper()
    return _code(text)


def _extract_point(segment: Mapping[str, Any], kind: str) -> str | None:
    if kind == "origin":
        keys = (
            "origin_code",
            "from_code",
            "departure_code",
            "departure_airport_code",
            "departure_station_code",
            "origin",
            "from",
            "departure",
            "departure_airport",
            "departure_station",
        )
    else:
        keys = (
            "destination_code",
            "to_code",
            "arrival_code",
            "arrival_airport_code",
            "arrival_station_code",
            "destination",
            "to",
            "arrival",
            "arrival_airport",
            "arrival_station",
        )
    for key in keys:
        result = _point_code(segment.get(key))
        if result:
            return result
    return None


def _extract_time(segment: Mapping[str, Any], kind: str) -> str | None:
    if kind == "departure":
        keys = (
            "departure_at",
            "departure_datetime",
            "departure_date_time",
            "departure_time",
            "depart_at",
            "departure",
            "from",
        )
    else:
        keys = (
            "arrival_at",
            "arrival_datetime",
            "arrival_date_time",
            "arrival_time",
            "arrive_at",
            "arrival",
            "to",
        )
    for key in keys:
        value = segment.get(key)
        if isinstance(value, Mapping):
            value = _first(value, "at", "datetime", "date_time", "time", "local_time", "timestamp")
        text = _clean_text(value)
        if text:
            return text
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-like timestamp while preserving unknown values as unknown."""

    if isinstance(value, datetime):
        return value
    text = _clean_text(value)
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # A common human/MCP representation with a space is covered by
        # fromisoformat; deliberately do not guess dates from bare clock times.
        return None


def _extract_carrier(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = _first(value, "name", "code", "iata", "carrier_code", "title")
    text = _clean_text(value)
    return text.upper() if text else None


def _extract_service_number(segment: Mapping[str, Any]) -> str | None:
    value = _first(
        segment,
        "service_number",
        "flight_number",
        "voyage_no",
        "train_number",
        "bus_number",
        "number",
        "flight",
    )
    if isinstance(value, Mapping):
        value = _first(value, "number", "code", "value")
    text = _clean_text(value)
    return text.upper().replace(" ", "") if text else None


def _extract_price(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, Mapping):
        amount = _first(value, "amount", "value", "price", "total")
        currency = _clean_text(_first(value, "currency", "currency_code"))
    else:
        amount = value
        currency = None
    try:
        if amount is None or isinstance(amount, bool):
            return None, currency
        return float(amount), currency
    except (TypeError, ValueError):
        return None, currency


def _extract_checkout_ref(raw: Mapping[str, Any]) -> JSON | None:
    # A selected fare is more specific than an offer-level compact summary.
    for key in ("variant", "selected_variant", "best_offer", "fare"):
        nested = raw.get(key)
        if isinstance(nested, Mapping) and isinstance(nested.get("checkout_ref"), Mapping):
            return dict(nested["checkout_ref"])
    value = raw.get("checkout_ref")
    if isinstance(value, Mapping):
        return dict(value)
    return None


def checkout_ref_for_variant(
    raw_offer: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    mode: str | None = None,
) -> JSON | None:
    """Return the exact checkout ref for one returned fare family.

    Most product replies put an opaque ``checkout_ref`` directly on each
    variant.  Tutu aviation's current compact response documents another
    exact representation: copy the offer-level ref, override ``offer_hash``
    and ``service_class`` from the chosen variant, and retain all other
    fields (notably passenger and round-trip data).  This is an MCP-documented
    ref transformation, never locally generated URL data.
    """

    direct = variant.get("checkout_ref")
    if isinstance(direct, Mapping):
        return dict(direct)

    family = _transport_family(raw_offer, mode=mode)
    parent = raw_offer.get("checkout_ref")
    # This override exists in Tutu's *avia* playbook only.  Do not generalise
    # it to rail, buses or hotels merely because they happen to contain an
    # ``offer_hash``-like field.
    if family == "avia":
        offer_hash = _clean_text(variant.get("offer_hash"))
        service_class = _clean_text(variant.get("service_class"))
        if not isinstance(parent, Mapping) or not offer_hash or not service_class:
            return None
        exact = dict(parent)
        exact["offer_hash"] = offer_hash
        exact["service_class"] = service_class
        return exact
    # Compact rail/bus often lists dummy variants without their own ref.
    # The offer-level checkout_ref is the exact handoff Tutu returned.
    if family in {"rail", "bus"} and isinstance(parent, Mapping):
        return dict(parent)
    return None


def _extract_bool(raw: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "included", "confirmed"}:
                return True
            if normalized in {"false", "no", "0", "not_included", "not included"}:
                return False
    return None


@dataclass(frozen=True)
class Segment:
    origin_code: str | None
    destination_code: str | None
    departure_at: str | None
    arrival_at: str | None
    carrier: str | None = None
    service_number: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def origin_label(self) -> str | None:
        return _clean_text(self.raw.get("from") or self.raw.get("origin") or self.raw.get("departure_station"))

    @property
    def destination_label(self) -> str | None:
        return _clean_text(self.raw.get("to") or self.raw.get("destination") or self.raw.get("arrival_station"))

    @property
    def departure_datetime(self) -> datetime | None:
        return parse_datetime(self.departure_at)

    @property
    def arrival_datetime(self) -> datetime | None:
        return parse_datetime(self.arrival_at)

    def signature(self, mode: str | None = None) -> JSON:
        """The concrete service identity used to prove a shared last leg."""

        return {
            "mode": mode,
            "carrier": self.carrier,
            "service_number": self.service_number,
            "origin_code": self.origin_code,
            "destination_code": self.destination_code,
            "departure_at": self.departure_at,
            "arrival_at": self.arrival_at,
        }

    def to_dict(self) -> JSON:
        payload: JSON = {
            "origin_code": self.origin_code,
            "destination_code": self.destination_code,
            "departure_at": self.departure_at,
            "arrival_at": self.arrival_at,
            "carrier": self.carrier,
            "service_number": self.service_number,
        }
        origin_name = self.origin_label
        destination_name = self.destination_label
        if origin_name:
            payload["origin_name"] = origin_name
        if destination_name:
            payload["destination_name"] = destination_name
        return payload


@dataclass(frozen=True)
class BaggageInfo:
    included: bool | None
    checked_pieces: int | None
    through_checked: bool | None
    source: str | None = None

    def to_dict(self) -> JSON:
        return {
            "included": self.included,
            "checked_pieces": self.checked_pieces,
            "through_checked": self.through_checked,
            "source": self.source,
        }


def _condition_candidates(raw: Mapping[str, Any]) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    # A selected variant is the exact fare the user can purchase.  Its
    # conditions must win over an offer-level compact summary.
    for selected_key in ("variant", "selected_variant", "fare", "best_offer"):
        selected = raw.get(selected_key)
        if isinstance(selected, Mapping):
            candidates.extend((f"{selected_key}.{source}", value) for source, value in _condition_candidates(selected))
    for prefix, item in (("offer", raw), ("conditions", raw.get("conditions"))):
        if isinstance(item, Mapping):
            for key in (
                "baggage",
                "checked_baggage",
                "baggage_allowance",
                "luggage",
                "checked_luggage",
            ):
                if key in item:
                    candidates.append((f"{prefix}.{key}", item[key]))
    return candidates


def _parse_baggage_value(value: Any) -> tuple[bool | None, int | None]:
    if isinstance(value, bool):
        return value, (1 if value else 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        pieces = max(0, int(value))
        return pieces > 0, pieces
    if isinstance(value, Mapping):
        included = _extract_bool(value, "included", "is_included", "available", "checked_included")
        amount = _first(value, "checked_pieces", "pieces", "count", "quantity", "amount", "value")
        pieces: int | None = None
        try:
            if amount is not None and not isinstance(amount, bool):
                pieces = max(0, int(float(amount)))
        except (TypeError, ValueError):
            pieces = None
        if included is None and pieces is not None:
            included = pieces > 0
        # Nested human-readable content must not override explicit false.
        if included is None:
            nested = _first(value, "label", "text", "description", "name")
            parsed_included, parsed_pieces = _parse_baggage_value(nested)
            included = parsed_included
            pieces = pieces if pieces is not None else parsed_pieces
        return included, pieces
    text = _clean_text(value)
    if not text:
        return None, None
    lower = text.lower()
    if any(token in lower for token in ("без багажа", "no checked", "not included", "не включ")):
        return False, 0
    # "ручная кладь" by itself is not a confirmed checked bag.
    if "ручн" in lower and "багаж" not in lower:
        return None, None
    match = re.search(r"(\d+)\s*(?:pc|pcs|piece|мест|шт)", lower)
    if match:
        pieces = int(match.group(1))
        return pieces > 0, pieces
    if "багаж" in lower or "checked" in lower:
        return True, None
    return None, None


def extract_baggage_info(raw: Mapping[str, Any]) -> BaggageInfo:
    """Extract only what the payload confirms; missing is never turned false."""

    included: bool | None = None
    pieces: int | None = None
    source: str | None = None
    for candidate_source, value in _condition_candidates(raw):
        parsed_included, parsed_pieces = _parse_baggage_value(value)
        if parsed_included is not None or parsed_pieces is not None:
            included = parsed_included if parsed_included is not None else included
            pieces = parsed_pieces if parsed_pieces is not None else pieces
            source = candidate_source
            # Candidate order starts with selected_variant / exact fare.  The
            # first evidence-backed value is therefore authoritative.
            break

    through_checked: bool | None = None
    through_keys = ("through_checked_baggage", "baggage_through_checked", "through_baggage_confirmed")
    for selected_key in ("variant", "selected_variant", "fare", "best_offer"):
        selected = raw.get(selected_key)
        if not isinstance(selected, Mapping):
            continue
        through_checked = _extract_bool(selected, *through_keys)
        selected_conditions = selected.get("conditions")
        if through_checked is None and isinstance(selected_conditions, Mapping):
            through_checked = _extract_bool(selected_conditions, *through_keys)
        if through_checked is not None:
            break
    if through_checked is None:
        through_checked = _extract_bool(raw, *through_keys)
    conditions = raw.get("conditions")
    if through_checked is None and isinstance(conditions, Mapping):
        through_checked = _extract_bool(conditions, *through_keys)
    return BaggageInfo(included, pieces, through_checked, source)


def _flatten_segments(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    segments: list[Mapping[str, Any]] = []
    direct = raw.get("segments")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        segments.extend(item for item in direct if isinstance(item, Mapping))
    legs = raw.get("legs")
    if isinstance(legs, Sequence) and not isinstance(legs, (str, bytes)):
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            leg_segments = leg.get("segments")
            if isinstance(leg_segments, Sequence) and not isinstance(leg_segments, (str, bytes)):
                segments.extend(item for item in leg_segments if isinstance(item, Mapping))
            elif any(key in leg for key in ("origin", "from", "departure", "departure_at")):
                segments.append(leg)
    route = raw.get("route")
    if not segments and isinstance(route, Mapping):
        nested = route.get("segments")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            segments.extend(item for item in nested if isinstance(item, Mapping))
    return segments


def _offer_id(raw: Mapping[str, Any], fallback: str) -> str:
    for selected_key in ("variant", "selected_variant", "fare", "best_offer"):
        selected = raw.get(selected_key)
        if isinstance(selected, Mapping):
            for key in ("id", "offer_id", "offer_hash", "hash", "uid", "service_class"):
                value = _clean_text(selected.get(key))
                if value:
                    return value
    for key in ("id", "offer_id", "offer_hash", "hash", "uid"):
        value = _clean_text(raw.get(key))
        if value:
            return value
    checkout_ref = raw.get("checkout_ref")
    if isinstance(checkout_ref, Mapping):
        for key in ("offer_hash", "id", "offer_id", "service_class"):
            value = _clean_text(checkout_ref.get(key))
            if value:
                return value
    return fallback


def _offer_city(raw: Mapping[str, Any], side: str) -> str | None:
    """City of a whole offer, e.g. a bus ride between two named stops.

    Bus segments only name the stop (``Автовокзал``); the city lives at offer
    level, in ``checkout_ref`` or ``meta``.
    """

    keys = (
        (f"city_{side}", f"{side}_city")
        if side in {"from", "to"}
        else ()
    )
    sources: list[Mapping[str, Any]] = [raw]
    for key in ("checkout_ref", "details_ref", "meta"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    aliases = keys + (
        ("departure_city", "origin_city") if side == "from" else ("arrival_city", "destination_city")
    )
    for source in sources:
        for key in aliases:
            value = _clean_text(source.get(key))
            if value and not value.isdigit():
                return value
    return None


@dataclass(frozen=True)
class TravelOffer:
    id: str
    variant_id: str | None
    mode: str
    segments: tuple[Segment, ...]
    price_amount: float | None
    currency: str | None
    checkout_ref: JSON | None
    is_multi_pnr: bool | None
    baggage: BaggageInfo
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def first_segment(self) -> Segment | None:
        return self.segments[0] if self.segments else None

    @property
    def last_segment(self) -> Segment | None:
        return self.segments[-1] if self.segments else None

    @property
    def origin_city(self) -> str | None:
        return _offer_city(self.raw, "from")

    @property
    def destination_city(self) -> str | None:
        return _offer_city(self.raw, "to")

    def to_dict(self, *, include_checkout_ref: bool = True) -> JSON:
        value: JSON = {
            "id": self.id,
            "variant_id": self.variant_id,
            "mode": self.mode,
            "segments": [segment.to_dict() for segment in self.segments],
            "price": {"amount": self.price_amount, "currency": self.currency},
            "is_multi_pnr": self.is_multi_pnr,
            "baggage": self.baggage.to_dict(),
        }
        origin_city = self.origin_city
        destination_city = self.destination_city
        if origin_city:
            value["origin_city"] = origin_city
        if destination_city:
            value["destination_city"] = destination_city
        if include_checkout_ref:
            value["checkout_ref"] = self.checkout_ref
        return value


def normalize_offer(raw: Mapping[str, Any], *, fallback_id: str = "offer", mode: str | None = None) -> TravelOffer:
    """Normalize a Tutu-like offer while retaining only evidence-backed facts."""

    if not isinstance(raw, Mapping):
        raise TypeError("offer must be a JSON object")
    raw_segments = _flatten_segments(raw)
    segments: list[Segment] = []
    for segment in raw_segments:
        carrier = _extract_carrier(_first(segment, "carrier", "airline", "operator", "operating_carrier"))
        segments.append(
            Segment(
                origin_code=_extract_point(segment, "origin"),
                destination_code=_extract_point(segment, "destination"),
                departure_at=_extract_time(segment, "departure"),
                arrival_at=_extract_time(segment, "arrival"),
                carrier=carrier,
                service_number=_extract_service_number(segment),
                raw=dict(segment),
            )
        )
    variant = raw.get("selected_variant") or raw.get("variant") or raw.get("fare")
    if isinstance(variant, Mapping):
        price_amount, currency = _extract_price(_first(variant, "price", "total_price", "price_total"))
    else:
        price_amount, currency = None, None
    if price_amount is None:
        price_amount, raw_currency = _extract_price(_first(raw, "price", "total_price", "price_total"))
        currency = currency or raw_currency
    actual_mode = _clean_text(
        mode or raw.get("mode") or raw.get("transport") or raw.get("transport_type") or raw.get("type")
    ) or "unknown"
    return TravelOffer(
        id=_offer_id(raw, fallback_id),
        variant_id=_variant_id_from_raw(raw),
        mode=actual_mode.lower(),
        segments=tuple(segments),
        price_amount=price_amount,
        currency=currency,
        checkout_ref=_extract_checkout_ref(raw),
        is_multi_pnr=_extract_bool(raw, "is_multi_pnr", "multi_pnr", "is_self_transfer"),
        baggage=extract_baggage_info(raw),
        raw=dict(raw),
    )


def _variant_id_from_raw(raw: Mapping[str, Any]) -> str | None:
    selected = raw.get("selected_variant") or raw.get("variant") or raw.get("fare")
    if not isinstance(selected, Mapping):
        return None
    for key in ("variant_id", "id", "offer_hash", "service_class", "code", "name"):
        value = _clean_text(selected.get(key))
        if value:
            return value
    return None


def _transport_family(raw: Mapping[str, Any] | None, *, mode: str | None = None) -> str:
    text = str(
        (raw or {}).get("transport")
        or (raw or {}).get("mode")
        or (raw or {}).get("product_type")
        or mode
        or ""
    ).lower()
    if any(token in text for token in ("avia", "air", "flight", "самол")):
        return "avia"
    if any(token in text for token in ("bus", "coach", "автобус")):
        return "bus"
    if any(token in text for token in ("rail", "train", "etrain", "поезд", "жд")):
        return "rail"
    return text or "unknown"


def expand_offer_variants(
    raw: Mapping[str, Any],
    *,
    fallback_id: str = "offer",
    mode: str | None = None,
) -> list[TravelOffer]:
    """Turn an offer's tariff list into exact, independently checkable fares.

    A top-level checkout reference is never blindly copied into multiple avia
    fare families.  Rail/bus compact variants may reuse the offer-level ref
    when Tutu did not attach a per-variant handle.
    """

    if raw.get("selected_variant") is not None or raw.get("variant") is not None:
        return [normalize_offer(raw, fallback_id=fallback_id, mode=mode)]
    variants = raw.get("variants")
    if not isinstance(variants, Sequence) or isinstance(variants, (str, bytes)):
        return [normalize_offer(raw, fallback_id=fallback_id, mode=mode)]
    expanded: list[TravelOffer] = []
    family = _transport_family(raw, mode=mode)
    for index, item in enumerate(variants, 1):
        if not isinstance(item, Mapping):
            continue
        selected_raw = dict(raw)
        # Remove the generic ref first.  Restore only an exact fare ref; for
        # aviation this is the MCP-documented variant override, not a guess.
        selected_raw.pop("checkout_ref", None)
        selected_raw["selected_variant"] = dict(item)
        exact_ref = checkout_ref_for_variant(raw, item, mode=mode)
        if exact_ref is None and family in {"rail", "bus"}:
            parent = raw.get("checkout_ref")
            if isinstance(parent, Mapping):
                exact_ref = dict(parent)
        if exact_ref is not None:
            selected_raw["checkout_ref"] = exact_ref
        expanded.append(normalize_offer(selected_raw, fallback_id=f"{fallback_id}-variant-{index}", mode=mode))
    return expanded or [normalize_offer(raw, fallback_id=fallback_id, mode=mode)]


def normalize_offers(raw_offers: Iterable[Mapping[str, Any]], *, mode: str | None = None, prefix: str = "offer") -> list[TravelOffer]:
    normalized: list[TravelOffer] = []
    for index, raw in enumerate(raw_offers, 1):
        normalized.extend(expand_offer_variants(raw, fallback_id=f"{prefix}-{index}", mode=mode))
    return normalized


def _integer(value: Any, default: int = 0) -> int:
    try:
        if value is None or isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Participant:
    id: str
    origin_code: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int) -> "Participant":
        identifier = _clean_text(_first(value, "id", "participant_id", "name", "key")) or f"participant-{index}"
        origin = _point_code(_first(value, "origin_code", "origin", "from", "city_code"))
        return cls(identifier, origin)

    def to_dict(self) -> JSON:
        return {"id": self.id, "origin_code": self.origin_code}


def _minutes(value: Any, *, default: int) -> int:
    if isinstance(value, timedelta):
        return int(value.total_seconds() // 60)
    return _integer(value, default)


@dataclass(frozen=True)
class GroupTripContract:
    participants: tuple[Participant, ...]
    hub_code: str
    destination_code: str
    min_wait_minutes: int = 0
    max_wait_minutes: int = 24 * 60
    required_checked_baggage_pieces: int = 0
    strict_baggage: bool = False
    expected_common_signature: Mapping[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GroupTripContract":
        raw_participants = value.get("participants")
        participants: list[Participant] = []
        if isinstance(raw_participants, Sequence) and not isinstance(raw_participants, (str, bytes)):
            participants = [Participant.from_mapping(item, index) for index, item in enumerate(raw_participants, 1) if isinstance(item, Mapping)]
        elif isinstance(value.get("origins"), Mapping):
            participants = [
                Participant(str(identifier), _point_code(origin))
                for identifier, origin in value["origins"].items()
            ]
        hub = _point_code(_first(value, "hub_code", "hub", "meeting_hub", "meeting_point"))
        destination = _point_code(_first(value, "destination_code", "destination", "to"))
        if not participants:
            raise ValueError("GroupTripContract requires at least one participant")
        if not hub:
            raise ValueError("GroupTripContract requires an exact hub_code (for example IST, not just Istanbul)")
        if not destination:
            raise ValueError("GroupTripContract requires an exact destination_code")
        minimum = _minutes(_first(value, "min_wait_minutes", "min_connection_minutes", "min_wait"), default=0)
        maximum = _minutes(_first(value, "max_wait_minutes", "max_connection_minutes", "max_wait"), default=24 * 60)
        if minimum < 0 or maximum < 0 or minimum > maximum:
            raise ValueError("connection window must satisfy 0 <= min_wait_minutes <= max_wait_minutes")
        baggage = _integer(
            _first(value, "required_checked_baggage_pieces", "checked_baggage_pieces", "baggage_pieces"),
            0,
        )
        expected_signature = value.get("expected_common_signature")
        if expected_signature is not None and not isinstance(expected_signature, Mapping):
            raise ValueError("expected_common_signature must be a JSON object")
        return cls(
            participants=tuple(participants),
            hub_code=hub,
            destination_code=destination,
            min_wait_minutes=minimum,
            max_wait_minutes=maximum,
            required_checked_baggage_pieces=max(0, baggage),
            strict_baggage=bool(value.get("strict_baggage", False)),
            expected_common_signature=dict(expected_signature) if expected_signature else None,
        )

    def to_dict(self) -> JSON:
        return {
            "participants": [participant.to_dict() for participant in self.participants],
            "hub_code": self.hub_code,
            "destination_code": self.destination_code,
            "min_wait_minutes": self.min_wait_minutes,
            "max_wait_minutes": self.max_wait_minutes,
            "required_checked_baggage_pieces": self.required_checked_baggage_pieces,
            "strict_baggage": self.strict_baggage,
            "expected_common_signature": dict(self.expected_common_signature) if self.expected_common_signature else None,
        }


@dataclass(frozen=True)
class RiskFinding:
    code: str
    status: str
    message: str
    evidence: JSON = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid risk status {self.status!r}")

    def to_dict(self) -> JSON:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "evidence": self.evidence,
        }


def overall_status(findings: Sequence[RiskFinding]) -> str:
    statuses = {finding.status for finding in findings}
    if STATUS_RISK in statuses:
        return STATUS_RISK
    if STATUS_UNKNOWN in statuses:
        return STATUS_UNKNOWN
    return STATUS_PASS


def _time_difference_minutes(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        return None
    return int((end - start).total_seconds() // 60)


def _crosses_night(start: datetime | None, end: datetime | None) -> bool | None:
    """True if a waiting interval touches local 00:00–05:59.

    This is a structural discomfort signal, not a prediction about sleep,
    lounges, delays, or airport opening hours.
    """

    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None) or end < start:
        return None
    # Short intervals are common; a bounded minute scan is unambiguous even
    # over midnight and accepts both aware and naive timestamps consistently.
    cursor = start.replace(second=0, microsecond=0)
    final = end.replace(second=0, microsecond=0)
    max_steps = max(1, min(7 * 24 * 60, int((final - cursor).total_seconds() // 60) + 2))
    for _ in range(max_steps):
        if 0 <= cursor.hour < 6:
            return True
        if cursor >= final:
            return False
        cursor += timedelta(minutes=1)
    return None


def _airport_change_findings(offer: TravelOffer) -> list[RiskFinding]:
    if len(offer.segments) < 2:
        return [RiskFinding("airport_change", STATUS_PASS, "Внутренней смены аэропорта/станции нет.")]
    findings: list[RiskFinding] = []
    for index, (previous, following) in enumerate(zip(offer.segments, offer.segments[1:]), 1):
        if not previous.destination_code or not following.origin_code:
            findings.append(
                RiskFinding(
                    "airport_change",
                    STATUS_UNKNOWN,
                    "Точка пересадки не подтверждена в данных Туту.",
                    {"connection_index": index},
                )
            )
        elif previous.destination_code != following.origin_code:
            findings.append(
                RiskFinding(
                    "airport_change",
                    STATUS_RISK,
                    "Маршрут меняет аэропорт/станцию на пересадке.",
                    {
                        "connection_index": index,
                        "arrival_point": previous.destination_code,
                        "departure_point": following.origin_code,
                    },
                )
            )
    if not findings:
        findings.append(RiskFinding("airport_change", STATUS_PASS, "Все внутренние пересадки в одной точке."))
    return findings


def _internal_night_findings(offer: TravelOffer) -> list[RiskFinding]:
    if len(offer.segments) < 2:
        return [RiskFinding("night_connection", STATUS_PASS, "Внутренней пересадки нет.")]
    findings: list[RiskFinding] = []
    for index, (previous, following) in enumerate(zip(offer.segments, offer.segments[1:]), 1):
        state = _crosses_night(previous.arrival_datetime, following.departure_datetime)
        evidence = {
            "connection_index": index,
            "arrival_at": previous.arrival_at,
            "departure_at": following.departure_at,
        }
        if state is True:
            findings.append(RiskFinding("night_connection", STATUS_RISK, "Пересадка затрагивает ночные часы (00:00–06:00).", evidence))
        elif state is False:
            findings.append(RiskFinding("night_connection", STATUS_PASS, "Внутренняя пересадка не затрагивает ночные часы.", evidence))
        else:
            findings.append(RiskFinding("night_connection", STATUS_UNKNOWN, "Время внутренней пересадки не удалось подтвердить.", evidence))
    return findings


def inspect_offer_risks(
    offer: Mapping[str, Any] | TravelOffer,
    *,
    required_checked_baggage_pieces: int = 0,
) -> JSON:
    """Inspect one selected offer; never infer absent fare facts.

    ``pass`` means a particular fact is present in the payload, ``risk`` means
    a fact known to increase purchase/transfer risk, and ``unknown`` means the
    MCP payload did not let us prove it either way.
    """

    normalized = offer if isinstance(offer, TravelOffer) else normalize_offer(offer)
    findings: list[RiskFinding] = []
    if normalized.is_multi_pnr is True:
        findings.append(
            RiskFinding(
                "self_transfer",
                STATUS_RISK,
                "Это отдельные билеты (self-transfer): стыковка не защищена, сквозной багаж не подтверждён.",
                {"is_multi_pnr": True},
            )
        )
    elif normalized.is_multi_pnr is False:
        findings.append(RiskFinding("self_transfer", STATUS_PASS, "Отдельные билеты в ответе Туту не отмечены.", {"is_multi_pnr": False}))
    else:
        findings.append(RiskFinding("self_transfer", STATUS_UNKNOWN, "Туту не вернул признак отдельных билетов.", {}))

    findings.extend(_airport_change_findings(normalized))
    findings.extend(_internal_night_findings(normalized))

    required = max(0, int(required_checked_baggage_pieces))
    baggage = normalized.baggage
    if required == 0:
        findings.append(RiskFinding("checked_baggage", STATUS_PASS, "Зарегистрированный багаж для этого запроса не обязателен.", {}))
    elif baggage.checked_pieces is not None:
        if baggage.checked_pieces >= required:
            findings.append(
                RiskFinding(
                    "checked_baggage",
                    STATUS_PASS,
                    "Количество зарегистрированного багажа подтверждено тарифом.",
                    {"required_pieces": required, "confirmed_pieces": baggage.checked_pieces, "source": baggage.source},
                )
            )
        else:
            findings.append(
                RiskFinding(
                    "checked_baggage",
                    STATUS_RISK,
                    "Тариф подтверждённо не включает нужное число мест багажа.",
                    {"required_pieces": required, "confirmed_pieces": baggage.checked_pieces, "source": baggage.source},
                )
            )
    elif baggage.included is True:
        findings.append(
            RiskFinding(
                "checked_baggage",
                STATUS_UNKNOWN,
                "Багаж упомянут, но число мест не подтверждено текущим ответом Туту.",
                {"required_pieces": required, "source": baggage.source},
            )
        )
    elif baggage.included is False:
        findings.append(
            RiskFinding(
                "checked_baggage",
                STATUS_RISK,
                "Тариф подтверждённо не включает зарегистрированный багаж.",
                {"required_pieces": required, "source": baggage.source},
            )
        )
    else:
        findings.append(
            RiskFinding(
                "checked_baggage",
                STATUS_UNKNOWN,
                "Туту не вернул условия зарегистрированного багажа для выбранного тарифа.",
                {"required_pieces": required},
            )
        )

    return {
        "offer_id": normalized.id,
        "overall_status": overall_status(findings),
        "findings": [finding.to_dict() for finding in findings],
    }


def inspect_connection_risks(
    feeder: Mapping[str, Any] | TravelOffer,
    common_offer: Mapping[str, Any] | TravelOffer,
    *,
    required_checked_baggage_pieces: int = 0,
    common_segment: Segment | None = None,
    joint_through_checked_baggage: bool | None = None,
) -> JSON:
    """Check the handoff between two independently selected offers.

    Same airline and two per-offer baggage flags are intentionally **not**
    proof of through-checked baggage across two checkout transactions.  Only
    an explicit joint proof supplied by a higher-level integration can produce
    ``pass`` for that fact.
    """

    feeder_offer = feeder if isinstance(feeder, TravelOffer) else normalize_offer(feeder)
    common = common_offer if isinstance(common_offer, TravelOffer) else normalize_offer(common_offer)
    findings: list[RiskFinding] = []
    feeder_last = feeder_offer.last_segment
    shared_segment = common_segment or common.first_segment
    if feeder_last is None or shared_segment is None:
        findings.append(RiskFinding("meeting_window", STATUS_UNKNOWN, "Не хватает сегментов для проверки стыковки.", {}))
    else:
        wait = _time_difference_minutes(feeder_last.arrival_datetime, shared_segment.departure_datetime)
        if wait is None:
            findings.append(
                RiskFinding(
                    "meeting_window",
                    STATUS_UNKNOWN,
                    "Время стыковки не удалось посчитать из текущих данных.",
                    {"feeder_arrival_at": feeder_last.arrival_at, "common_departure_at": shared_segment.departure_at},
                )
            )
        elif wait < 0:
            findings.append(RiskFinding("meeting_window", STATUS_RISK, "Общий сегмент отправляется раньше прибытия участника.", {"wait_minutes": wait}))
        else:
            # A user may deliberately permit a short meeting window.  That
            # makes it eligible for the hard filter, but must not turn it into
            # a silent endorsement: these are independently purchased
            # components, so a small structural buffer is a real trade-off,
            # not a delay prediction.
            if wait < 4 * 60:
                findings.append(
                    RiskFinding(
                        "short_connection_buffer",
                        STATUS_RISK,
                        "Запас между отдельными оформлениями меньше 4 часов. Это не прогноз задержки, но при сдвиге первого рейса стыковку придётся решать самостоятельно.",
                        {"wait_minutes": wait, "recommended_minimum_minutes": 4 * 60},
                    )
                )
            else:
                findings.append(
                    RiskFinding(
                        "short_connection_buffer",
                        STATUS_PASS,
                        "Структурный запас между отдельными оформлениями — не менее 4 часов.",
                        {"wait_minutes": wait, "recommended_minimum_minutes": 4 * 60},
                    )
                )
            night = _crosses_night(feeder_last.arrival_datetime, shared_segment.departure_datetime)
            if night is True:
                findings.append(RiskFinding("night_wait", STATUS_RISK, "Ожидание общей услуги затрагивает ночные часы.", {"wait_minutes": wait}))
            elif night is False:
                findings.append(RiskFinding("night_wait", STATUS_PASS, "Ожидание общей услуги не затрагивает ночные часы.", {"wait_minutes": wait}))
            else:
                findings.append(RiskFinding("night_wait", STATUS_UNKNOWN, "Ночной риск ожидания нельзя подтвердить без полных времён.", {"wait_minutes": wait}))

    if required_checked_baggage_pieces > 0:
        if feeder_offer.is_multi_pnr is True or common.is_multi_pnr is True:
            findings.append(
                RiskFinding(
                    "through_checked_baggage",
                    STATUS_RISK,
                    "В одном из офферов подтверждены отдельные билеты: багаж придётся получать и сдавать заново.",
                    {"feeder_is_multi_pnr": feeder_offer.is_multi_pnr, "common_is_multi_pnr": common.is_multi_pnr},
                )
            )
        elif joint_through_checked_baggage is True:
            findings.append(
                RiskFinding(
                    "through_checked_baggage",
                    STATUS_PASS,
                    "Сквозная регистрация подтверждена именно для этой пары покупок.",
                    {"joint_through_checked_baggage": True},
                )
            )
        elif joint_through_checked_baggage is False:
            findings.append(RiskFinding("through_checked_baggage", STATUS_RISK, "Оффер подтверждает, что багаж не оформляется сквозь стыковку.", {}))
        else:
            same_carrier = bool(
                feeder_last
                and shared_segment
                and feeder_last.carrier
                and feeder_last.carrier == shared_segment.carrier
            )
            guidance = (
                " Если оба плеча оформляет один перевозчик, спросите на регистрации, "
                "может ли он оформить багаж сразу до конечной точки; текущие данные этого не подтверждают."
                if same_carrier
                else ""
            )
            findings.append(
                RiskFinding(
                    "through_checked_baggage",
                    STATUS_UNKNOWN,
                    "Это отдельные оформления: даже два положительных признака в офферах не доказывают сквозную регистрацию багажа."
                    + guidance,
                    {
                        "feeder_carrier": feeder_last.carrier if feeder_last else None,
                        "common_carrier": shared_segment.carrier if shared_segment else None,
                        "same_carrier": same_carrier,
                        "feeder_offer_through_flag": feeder_offer.baggage.through_checked,
                        "common_offer_through_flag": common.baggage.through_checked,
                    },
                )
            )
    return {"overall_status": overall_status(findings), "findings": [finding.to_dict() for finding in findings]}


def _signature_matches_safe(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool | None:
    comparable = False
    codes = {"carrier", "service_number", "origin_code", "destination_code", "mode"}
    for key in ("mode", "carrier", "service_number", "origin_code", "destination_code", "departure_at", "arrival_at"):
        wanted = expected.get(key)
        if wanted is None:
            continue
        comparable = True
        left = _code(wanted) if key in codes else wanted
        right = _code(actual.get(key)) if key in codes else actual.get(key)
        if left != right:
            return False
    return True if comparable else None


def _endpoint_matches(requested: str, offer: TravelOffer, *, side: str, mode: str) -> bool:
    """Match a contract point against the offer's first or last endpoint."""

    segment = offer.first_segment if side == "origin" else offer.last_segment
    if segment is None:
        return False
    if side == "origin":
        code, labels = segment.origin_code, (segment.origin_label, offer.origin_city)
    else:
        code, labels = segment.destination_code, (segment.destination_label, offer.destination_city)
    return any(
        meeting_point_matches(requested, code=code, label=label, mode=mode)
        for label in (labels or (None,))
    )


def find_common_segment(offer: TravelOffer, contract: GroupTripContract) -> Segment | None:
    """Return the shared service that starts at the hub and ends at destination.

    It is not enough for an arbitrary *internal* segment to be ``IST → LHR``:
    a ticket such as ``VKO → IST → LHR`` cannot be used as the group component
    after people separately meet in IST.  Aviation therefore requires one
    exact direct flight.  Rail and bus may use city/station aliases for the
    same meeting city, and a single purchased ticket may include a connection.
    """

    if not offer.segments:
        return None
    first, last = offer.first_segment, offer.last_segment
    if first is None or last is None:
        return None
    mode = offer.mode or ""
    if not _endpoint_matches(contract.hub_code, offer, side="origin", mode=mode):
        return None
    if not _endpoint_matches(contract.destination_code, offer, side="destination", mode=mode):
        return None
    family = _transport_family({"mode": mode}, mode=mode)
    if family == "avia" and len(offer.segments) != 1:
        return None
    return first


def _offer_duration_minutes(offer: TravelOffer) -> int | None:
    first, last = offer.first_segment, offer.last_segment
    if not first or not last:
        return None
    return _time_difference_minutes(first.departure_datetime, last.arrival_datetime)


@dataclass(frozen=True)
class FeederCandidate:
    participant_id: str
    offer: TravelOffer
    wait_minutes: int
    connection_risks: JSON

    def to_dict(self) -> JSON:
        return {
            "participant_id": self.participant_id,
            "offer": self.offer.to_dict(),
            "wait_minutes": self.wait_minutes,
            "connection_risks": self.connection_risks,
        }


@dataclass(frozen=True)
class GroupScenario:
    common_offer: TravelOffer
    common_segment: Segment
    feeders: tuple[FeederCandidate, ...]
    risks: JSON
    total_price: float | None
    currency: str | None

    def to_dict(self) -> JSON:
        waits = [feeder.wait_minutes for feeder in self.feeders]
        feeder_durations = [duration for duration in (_offer_duration_minutes(feeder.offer) for feeder in self.feeders) if duration is not None]
        return {
            "common_offer": self.common_offer.to_dict(),
            "common_service_signature": self.common_segment.signature(self.common_offer.mode),
            "feeders": [feeder.to_dict() for feeder in self.feeders],
            "metrics": {
                "total_price": self.total_price,
                "currency": self.currency,
                "max_wait_minutes": max(waits) if waits else None,
                "min_wait_minutes": min(waits) if waits else None,
                "arrival_spread_minutes": (max(waits) - min(waits)) if waits else None,
                "common_duration_minutes": _offer_duration_minutes(self.common_offer),
                "max_feeder_duration_minutes": max(feeder_durations) if feeder_durations else None,
                "risk_count": sum(1 for item in self.risks["findings"] if item["status"] == STATUS_RISK),
                "unknown_count": sum(1 for item in self.risks["findings"] if item["status"] == STATUS_UNKNOWN),
            },
            "risks": self.risks,
        }


def _total_price(common: TravelOffer, feeders: Sequence[FeederCandidate]) -> tuple[float | None, str | None]:
    offers = [common, *(candidate.offer for candidate in feeders)]
    if any(offer.price_amount is None for offer in offers):
        return None, None
    if any(not offer.currency for offer in offers):
        return None, None
    currencies = {offer.currency for offer in offers if offer.currency}
    if len(currencies) > 1:
        return None, None
    return sum(float(offer.price_amount or 0) for offer in offers), next(iter(currencies), None)


def _scenario_risks(
    common: TravelOffer,
    common_segment: Segment,
    feeders: Sequence[FeederCandidate],
    contract: GroupTripContract,
) -> JSON:
    findings: list[RiskFinding] = []
    signature = common_segment.signature(common.mode)
    signature_fields = ("carrier", "service_number", "origin_code", "destination_code", "departure_at", "arrival_at")
    missing_signature_fields = [field for field in signature_fields if not signature.get(field)]
    if missing_signature_fields:
        findings.append(
            RiskFinding(
                "common_service_signature",
                STATUS_UNKNOWN,
                "Не все поля точного общего сегмента вернул текущий ответ Туту.",
                {"signature": signature, "missing_fields": missing_signature_fields},
            )
        )
    else:
        findings.append(
            RiskFinding(
                "common_service_signature",
                STATUS_PASS,
                "Точный общий сегмент подтверждён перевозчиком, номером, точками и временем.",
                {"signature": signature},
            )
        )
    common_result = inspect_offer_risks(common, required_checked_baggage_pieces=contract.required_checked_baggage_pieces)
    findings.extend(RiskFinding(**item) for item in common_result["findings"])
    for candidate in feeders:
        feeder_result = inspect_offer_risks(candidate.offer, required_checked_baggage_pieces=contract.required_checked_baggage_pieces)
        for item in feeder_result["findings"]:
            findings.append(
                RiskFinding(
                    code=f"{candidate.participant_id}.{item['code']}",
                    status=item["status"],
                    message=item["message"],
                    evidence=item["evidence"],
                )
            )
        for item in candidate.connection_risks["findings"]:
            findings.append(
                RiskFinding(
                    code=f"{candidate.participant_id}.{item['code']}",
                    status=item["status"],
                    message=item["message"],
                    evidence=item["evidence"],
                )
            )
    return {"overall_status": overall_status(findings), "findings": [finding.to_dict() for finding in findings]}


class GroupSyncSolver:
    """Pure solver for 2–4-person (and test-sized) rendezvous searches."""

    def __init__(self, *, max_feeders_per_participant: int = 6, max_combinations_per_common_offer: int = 512) -> None:
        self.max_feeders_per_participant = max(1, max_feeders_per_participant)
        self.max_combinations_per_common_offer = max(1, max_combinations_per_common_offer)

    def solve(
        self,
        contract: GroupTripContract | Mapping[str, Any],
        common_offers: Iterable[Mapping[str, Any] | TravelOffer],
        feeders_by_participant: Mapping[str, Iterable[Mapping[str, Any] | TravelOffer]],
        *,
        max_scenarios: int = 3,
    ) -> JSON:
        contract_value = contract if isinstance(contract, GroupTripContract) else GroupTripContract.from_mapping(contract)
        normalized_common: list[TravelOffer] = []
        for index, item in enumerate(common_offers, 1):
            if isinstance(item, TravelOffer):
                normalized_common.append(item)
            else:
                normalized_common.extend(expand_offer_variants(item, fallback_id=f"common-{index}"))
        normalized_feeders: dict[str, list[TravelOffer]] = {}
        for participant in contract_value.participants:
            source = feeders_by_participant.get(participant.id, [])
            normalized_feeders[participant.id] = []
            for index, item in enumerate(source, 1):
                if isinstance(item, TravelOffer):
                    normalized_feeders[participant.id].append(item)
                else:
                    normalized_feeders[participant.id].extend(
                        expand_offer_variants(item, fallback_id=f"{participant.id}-feeder-{index}")
                    )

        excluded: list[JSON] = []
        scenarios: list[GroupScenario] = []
        for common in normalized_common:
            common_segment = find_common_segment(common, contract_value)
            if common_segment is None:
                excluded.append({"scope": "common", "offer_id": common.id, "reason": "common_leg_not_exact_hub_to_destination"})
                continue
            signature = common_segment.signature(common.mode)
            expected = contract_value.expected_common_signature
            signature_match = _signature_matches_safe(signature, expected) if expected else True
            if signature_match is False:
                excluded.append({"scope": "common", "offer_id": common.id, "reason": "common_service_signature_mismatch", "actual": signature})
                continue

            candidates_per_person: list[list[FeederCandidate]] = []
            has_all = True
            for participant in contract_value.participants:
                candidates = self._feeder_candidates(
                    participant,
                    normalized_feeders[participant.id],
                    common,
                    common_segment,
                    contract_value,
                    excluded,
                )
                if not candidates:
                    has_all = False
                    excluded.append({"scope": "participant", "participant_id": participant.id, "reason": "no_feeder_within_exact_hub_window", "common_offer_id": common.id})
                    break
                candidates_per_person.append(candidates[: self.max_feeders_per_participant])
            if not has_all:
                continue

            for combination_index, combination in enumerate(product(*candidates_per_person)):
                if combination_index >= self.max_combinations_per_common_offer:
                    excluded.append({"scope": "solver", "offer_id": common.id, "reason": "combination_cap_reached"})
                    break
                feeder_candidates = tuple(combination)
                risks = _scenario_risks(common, common_segment, feeder_candidates, contract_value)
                # ``strict_baggage`` means every selected fare must include
                # the requested checked pieces.  It must not silently turn a
                # separate-purchase *through-baggage* unknown into a hard
                # rejection: that fact is shown as an explicit purchase risk
                # instead, because a traveller may still collect and recheck
                # their own confirmed bag at the hub.
                if contract_value.strict_baggage and any(
                    (item["code"] == "checked_baggage" or item["code"].endswith(".checked_baggage"))
                    and item["status"] != STATUS_PASS
                    for item in risks["findings"]
                ):
                    excluded.append(
                        {
                            "scope": "scenario",
                            "common_offer_id": common.id,
                            "reason": "strict_baggage_not_confirmed",
                            "participants": [candidate.participant_id for candidate in feeder_candidates],
                        }
                    )
                    continue
                price, currency = _total_price(common, feeder_candidates)
                scenarios.append(GroupScenario(common, common_segment, feeder_candidates, risks, price, currency))

        ranked = self._rank(scenarios, max_scenarios=max_scenarios)
        excluded_summary: dict[str, int] = {}
        for item in excluded:
            excluded_summary[item["reason"]] = excluded_summary.get(item["reason"], 0) + 1
        return {
            "contract": contract_value.to_dict(),
            "scenarios": [scenario.to_dict() for scenario in ranked],
            "excluded": excluded,
            "excluded_summary": excluded_summary,
            "search_status": "pass" if ranked else "unknown",
        }

    def _feeder_candidates(
        self,
        participant: Participant,
        feeders: Sequence[TravelOffer],
        common: TravelOffer,
        common_segment: Segment,
        contract: GroupTripContract,
        excluded: list[JSON],
    ) -> list[FeederCandidate]:
        candidates: list[FeederCandidate] = []
        common_departure = common_segment.departure_datetime
        for feeder in feeders:
            first, last = feeder.first_segment, feeder.last_segment
            if first is None or last is None:
                excluded.append({"scope": "feeder", "participant_id": participant.id, "offer_id": feeder.id, "reason": "missing_segments"})
                continue
            if participant.origin_code and first.origin_code and not _endpoint_matches(
                participant.origin_code, feeder, side="origin", mode=feeder.mode
            ):
                excluded.append({"scope": "feeder", "participant_id": participant.id, "offer_id": feeder.id, "reason": "wrong_origin"})
                continue
            if first.origin_code is None and participant.origin_code:
                excluded.append({"scope": "feeder", "participant_id": participant.id, "offer_id": feeder.id, "reason": "origin_unknown"})
                continue
            if not _endpoint_matches(contract.hub_code, feeder, side="destination", mode=feeder.mode):
                reason = "different_hub_airport_or_station" if last.destination_code else "hub_unknown"
                excluded.append(
                    {
                        "scope": "feeder",
                        "participant_id": participant.id,
                        "offer_id": feeder.id,
                        "reason": reason,
                        "actual_endpoint": last.destination_code,
                        "required_hub": contract.hub_code,
                    }
                )
                continue
            wait = _time_difference_minutes(last.arrival_datetime, common_departure)
            if wait is None:
                excluded.append({"scope": "feeder", "participant_id": participant.id, "offer_id": feeder.id, "reason": "meeting_window_unknown"})
                continue
            if wait < contract.min_wait_minutes or wait > contract.max_wait_minutes:
                excluded.append(
                    {
                        "scope": "feeder",
                        "participant_id": participant.id,
                        # The same feeder can be outside the window for one
                        # shared service but valid for another.  Keep this
                        # provenance so a later, deterministic constraint
                        # negotiator can only reason about the exact pair it
                        # re-validates.
                        "common_offer_id": common.id,
                        "offer_id": feeder.id,
                        "reason": "outside_meeting_window",
                        "wait_minutes": wait,
                        "min_wait_minutes": contract.min_wait_minutes,
                        "max_wait_minutes": contract.max_wait_minutes,
                    }
                )
                continue
            connection = inspect_connection_risks(
                feeder,
                common,
                required_checked_baggage_pieces=contract.required_checked_baggage_pieces,
                common_segment=common_segment,
            )
            candidates.append(FeederCandidate(participant.id, feeder, wait, connection))
        candidates.sort(key=self._feeder_sort_key)
        return candidates

    @staticmethod
    def _feeder_sort_key(candidate: FeederCandidate) -> tuple[float, int, int, str]:
        price = candidate.offer.price_amount if candidate.offer.price_amount is not None else math.inf
        risk_count = sum(1 for finding in candidate.connection_risks["findings"] if finding["status"] == STATUS_RISK)
        return (price, risk_count, candidate.wait_minutes, candidate.offer.id)

    @staticmethod
    def _rank(scenarios: Sequence[GroupScenario], *, max_scenarios: int) -> list[GroupScenario]:
        def key(scenario: GroupScenario) -> tuple[float, int, int, int, str]:
            total = scenario.total_price if scenario.total_price is not None else math.inf
            waits = [item.wait_minutes for item in scenario.feeders]
            max_wait = max(waits) if waits else math.inf
            spread = (max(waits) - min(waits)) if waits else math.inf
            durations = [duration for duration in (_offer_duration_minutes(candidate.offer) for candidate in scenario.feeders) if duration is not None]
            max_duration = max(durations) if durations else math.inf
            risk_count = sum(1 for item in scenario.risks["findings"] if item["status"] == STATUS_RISK)
            unknown_count = sum(1 for item in scenario.risks["findings"] if item["status"] == STATUS_UNKNOWN)
            return (risk_count, unknown_count, total, max_wait, f"{max_duration}:{spread}:{scenario.common_offer.id}")

        unique: list[GroupScenario] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for scenario in sorted(scenarios, key=key):
            identity = (scenario.common_offer.id, tuple(item.offer.id for item in scenario.feeders))
            if identity not in seen:
                seen.add(identity)
                unique.append(scenario)
            if len(unique) >= max(0, max_scenarios):
                break
        return unique


def solve_group_rendezvous(
    contract: GroupTripContract | Mapping[str, Any],
    common_offers: Iterable[Mapping[str, Any] | TravelOffer],
    feeders_by_participant: Mapping[str, Iterable[Mapping[str, Any] | TravelOffer]],
    *,
    max_scenarios: int = 3,
) -> JSON:
    """Convenience functional API for tool handlers and direct unit tests."""

    return GroupSyncSolver().solve(contract, common_offers, feeders_by_participant, max_scenarios=max_scenarios)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _passenger_counts(value: Mapping[str, Any]) -> JSON:
    aliases = {
        "passengers_full": ("passengers_full", "adults", "adult_count", "passengers_adult", "passengers"),
        "passengers_child": ("passengers_child", "children", "child_count"),
        "passengers_infant": ("passengers_infant", "infants", "infant_count"),
    }
    result: JSON = {}
    for canonical, keys in aliases.items():
        for key in keys:
            if key in value and value[key] is not None:
                if isinstance(value[key], (Mapping, list, tuple)):
                    continue
                parsed = _integer(value[key], -1)
                if parsed < 0:
                    continue
                result[canonical] = parsed
                break
    return result


def checkout_handoff_guard(
    offer: Mapping[str, Any] | TravelOffer,
    *,
    explicit_selection: bool,
    selected_checkout_ref: Mapping[str, Any] | None,
    expected_passengers: Mapping[str, Any] | None = None,
) -> JSON:
    """Prevent premature or mutated checkout handoffs.

    The returned ``checkout_ref`` is the original offer ref.  The function does
    not call Tutu and cannot create a link by itself.
    """

    normalized = offer if isinstance(offer, TravelOffer) else normalize_offer(offer)
    errors: list[str] = []
    if explicit_selection is not True:
        errors.append("explicit_fare_selection_required")
    if normalized.checkout_ref is None:
        errors.append("offer_has_no_checkout_ref")
    if selected_checkout_ref is None:
        errors.append("selected_checkout_ref_required")
    elif normalized.checkout_ref is not None:
        try:
            if _canonical_json(selected_checkout_ref) != _canonical_json(normalized.checkout_ref):
                errors.append("selected_checkout_ref_does_not_match_offer")
        except (TypeError, ValueError):
            errors.append("selected_checkout_ref_not_json_serializable")

    expected = _passenger_counts(expected_passengers or {})
    actual = _passenger_counts(normalized.checkout_ref or {})
    if actual:
        for key, expected_value in expected.items():
            actual_value = actual.get(key)
            if actual_value is None:
                continue
            if actual_value != expected_value:
                errors.append(f"checkout_ref_{key}_mismatch")
    return {
        "allowed": not errors,
        "errors": errors,
        "offer_id": normalized.id,
        "checkout_ref": normalized.checkout_ref if not errors else None,
        "passengers": actual,
    }

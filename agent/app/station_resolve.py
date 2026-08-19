"""Resolve city names to Tutu station strings without inventing geo ids.

The live model only sees ``plan_individual_trip`` / ``plan_group_sync``.  Those
handlers already fetch ``get_{mode}_instructions``.  This module reads station
examples out of that payload and, when a documented default pair is present in
the same text, uses it.  Search values always come from instruction content
(or from the caller's own exact code).  Hardcoded ids are matchers, never
synthetic origins.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any, Literal


JSON = dict[str, Any]
ResolutionStatus = Literal["exact", "resolved", "passthrough", "ambiguous"]

_STATION_RE = re.compile(
    r"(?P<label>"
    r"(?P<city>[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9.\- ]{1,40}?)"
    r"(?:\s*[—–]\s*|\s+-\s+)"
    r"(?P<name>[^()\n]{2,80}?)"
    r")\s*\((?P<code>\d{4,}|[A-Za-z]{3})\)"
)
_PAREN_CODE_RE = re.compile(r"\((\d{4,}|[A-Za-z]{3})\)")
_IATA_RE = re.compile(r"^[A-Za-z]{3}$")
_GEO_ID_RE = re.compile(r"^\d{4,}$")
_NON_ALNUM_RE = re.compile(r"[^a-zа-я0-9]+")

_MOSCOW = frozenset({"москва", "moscow", "mow"})
_SPB = frozenset(
    {
        "санктпетербург",
        "петербург",
        "питер",
        "saintpetersburg",
        "stpetersburg",
        "spb",
        "led",
        "спб",
    }
)
_KAZAN = frozenset({"казань", "kazan", "kzn"})
_CITY_GROUPS: tuple[frozenset[str], ...] = (_MOSCOW, _SPB, _KAZAN)

_MODE_LABELS = {"avia": "самолёт", "rail": "поезд", "bus": "автобус"}
_AMBIGUOUS_REJECTION = "Поиск не запускался: нужно уточнить станцию."
_TUTU_REJECTED_REJECTION = "Туту не принял точки маршрута. Укажите точную станцию или код."


@dataclass(frozen=True, slots=True)
class Station:
    search_value: str
    code: str
    city: str
    name: str


@dataclass(frozen=True, slots=True)
class DocumentedPair:
    mode: str
    left_cities: frozenset[str]
    left_needles: tuple[str, ...]
    right_cities: frozenset[str]
    right_needles: tuple[str, ...]


# Needles identify stations *inside instruction text*.  The search string is
# the matched instruction example, not a string we construct from these ids.
_DOCUMENTED_PAIRS: tuple[DocumentedPair, ...] = (
    DocumentedPair(
        mode="rail",
        left_cities=_MOSCOW,
        left_needles=("ленинградский", "2006004"),
        right_cities=_SPB,
        right_needles=("московский", "2004004"),
    ),
    DocumentedPair(
        mode="bus",
        left_cities=_KAZAN,
        left_needles=("южный",),
        right_cities=_MOSCOW,
        right_needles=("саларьево",),
    ),
)


@dataclass(frozen=True, slots=True)
class PointResolution:
    status: ResolutionStatus
    value: str
    candidates: tuple[Station, ...] = ()
    question: str | None = None

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"


@dataclass(frozen=True, slots=True)
class RouteResolution:
    origin: PointResolution
    destination: PointResolution
    question: str | None = None

    @property
    def ambiguous(self) -> bool:
        return self.origin.ambiguous or self.destination.ambiguous


def normalize_city(value: str) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    return _NON_ALNUM_RE.sub("", text)


def has_geo_code(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _IATA_RE.fullmatch(text) or _GEO_ID_RE.fullmatch(text):
        return True
    return _PAREN_CODE_RE.search(text) is not None


def is_exact_point(value: str) -> bool:
    """True when the caller already supplied IATA, a geo id, or a coded station."""

    text = str(value or "").strip()
    if not text:
        return False
    if _IATA_RE.fullmatch(text) or _GEO_ID_RE.fullmatch(text):
        return True
    return bool(_STATION_RE.search(text))


def point_tokens(value: str) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    tokens = {text.upper()}
    for match in _PAREN_CODE_RE.finditer(text):
        tokens.add(match.group(1).upper())
    if _IATA_RE.fullmatch(text) or _GEO_ID_RE.fullmatch(text):
        tokens.add(text.upper())
    return tokens


def same_city(left: str, right: str) -> bool:
    """True when two search/offer strings refer to the same known city."""

    a = normalize_city(left)
    b = normalize_city(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    for group in _CITY_GROUPS:
        left_hit = any(token == a or token in a for token in group)
        right_hit = any(token == b or token in b for token in group)
        if left_hit and right_hit:
            return True
    return False


def points_compatible(requested: str, observed: str) -> bool:
    if not str(requested or "").strip() or not str(observed or "").strip():
        return True
    if not has_geo_code(requested):
        # City-level search: trust Tutu's station-level offer codes.
        return True
    return bool(point_tokens(requested) & point_tokens(observed))


def extract_stations(payload: Any) -> tuple[Station, ...]:
    found: list[Station] = []
    seen: set[tuple[str, str]] = set()
    for text in _iter_strings(payload):
        for match in _STATION_RE.finditer(text):
            station = Station(
                search_value=match.group(0).strip(),
                code=match.group("code").upper(),
                city=match.group("city").strip(),
                name=match.group("name").strip(),
            )
            key = (station.code, normalize_city(station.search_value))
            if key in seen:
                continue
            seen.add(key)
            found.append(station)
    return tuple(found)


def resolve_route(origin: str, destination: str, *, mode: str, instructions: Any) -> RouteResolution:
    origin_text = str(origin or "").strip()
    destination_text = str(destination or "").strip()
    catalog = extract_stations(instructions)
    origin_res, destination_res = _resolve_pair(
        origin_text,
        destination_text,
        mode=mode,
        catalog=catalog,
    )
    question = None
    if origin_res.ambiguous or destination_res.ambiguous:
        question = _route_question(origin_text, destination_text, origin_res, destination_res, mode=mode)
        origin_res = PointResolution(
            status="ambiguous",
            value=origin_text,
            candidates=origin_res.candidates,
            question=question,
        )
        destination_res = PointResolution(
            status="ambiguous",
            value=destination_text,
            candidates=destination_res.candidates,
            question=question,
        )
    return RouteResolution(origin=origin_res, destination=destination_res, question=question)


def clarification_presentation(
    *,
    query: str,
    origin: str,
    destination: str,
    mode: str,
    departure_date: str,
    adults: int,
    summary: str,
    rejection_summary: str = _AMBIGUOUS_REJECTION,
    participants: Sequence[Mapping[str, Any]] | None = None,
) -> JSON:
    mode_label = _MODE_LABELS.get(mode, mode)
    people = [dict(item) for item in participants or ({"name": "Путешественник", "origin": origin},)]
    return {
        "query": query,
        "summary": summary,
        "contract": {
            "title": "Нужно уточнить станцию",
            "route": f"{origin} → {destination}",
            "participants": people,
            "hard_constraints": [
                {"label": f"Маршрут: {origin} → {destination}", "state": "unknown"},
                {"label": f"Дата: {departure_date}", "state": "confirmed"},
                {"label": f"Транспорт: {mode_label}", "state": "confirmed"},
                {"label": f"Пассажиры: {adults}", "state": "confirmed"},
            ],
            "soft_preferences": [
                "Поиск начнётся после выбора конкретной станции из ответа Туту.",
            ],
        },
        "timeline": [],
        "scenarios": [],
        "rejection_summary": rejection_summary,
    }


def tutu_search_error_question(origin: str, destination: str) -> str:
    return (
        f"Туту не принял маршрут {origin} → {destination}. "
        "Укажите точную станцию или код — без подстановки с нашей стороны."
    )


def tutu_search_error_rejection() -> str:
    return _TUTU_REJECTED_REJECTION


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _iter_strings(item)


def _city_group(value: str) -> frozenset[str] | None:
    token = normalize_city(value)
    if not token:
        return None
    for group in _CITY_GROUPS:
        if token in group:
            return group
    return None


def _same_city(left: str, right: str) -> bool:
    left_token = normalize_city(left)
    right_token = normalize_city(right)
    if not left_token or not right_token:
        return False
    if left_token == right_token:
        return True
    left_group = _city_group(left)
    right_group = _city_group(right)
    return left_group is not None and left_group is right_group


def _station_in_city(station: Station, city: str) -> bool:
    return _same_city(station.city, city)


def _stations_for_city(catalog: Sequence[Station], city: str) -> tuple[Station, ...]:
    return tuple(station for station in catalog if _station_in_city(station, city))


def _station_matches_needles(station: Station, needles: Sequence[str]) -> bool:
    haystack = f"{station.search_value} {station.name} {station.code}".lower().replace("ё", "е")
    return any(needle.lower() in haystack for needle in needles)


def _find_documented_pair(
    origin: str,
    destination: str,
    *,
    mode: str,
    catalog: Sequence[Station],
) -> tuple[Station, Station] | None:
    origin_group = _city_group(origin)
    dest_group = _city_group(destination)
    if origin_group is None or dest_group is None:
        return None
    for pair in _DOCUMENTED_PAIRS:
        if pair.mode != mode:
            continue
        if origin_group == pair.left_cities and dest_group == pair.right_cities:
            left = _pick_needled(catalog, origin, pair.left_needles)
            right = _pick_needled(catalog, destination, pair.right_needles)
        elif origin_group == pair.right_cities and dest_group == pair.left_cities:
            left = _pick_needled(catalog, origin, pair.right_needles)
            right = _pick_needled(catalog, destination, pair.left_needles)
        else:
            continue
        if left is not None and right is not None:
            return left, right
    return None


def _pick_needled(catalog: Sequence[Station], city: str, needles: Sequence[str]) -> Station | None:
    matches = [station for station in _stations_for_city(catalog, city) if _station_matches_needles(station, needles)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Identical codes from slightly different instruction spellings.
        codes = {station.code for station in matches}
        if len(codes) == 1:
            return matches[0]
    return None


def _resolve_one(value: str, *, catalog: Sequence[Station]) -> PointResolution:
    if is_exact_point(value):
        return PointResolution(status="exact", value=value)
    matches = _stations_for_city(catalog, value)
    if len(matches) == 1:
        return PointResolution(status="resolved", value=matches[0].search_value, candidates=matches)
    if len(matches) > 1:
        codes = {station.code for station in matches}
        if len(codes) == 1:
            return PointResolution(status="resolved", value=matches[0].search_value, candidates=matches)
        return PointResolution(
            status="ambiguous",
            value=value,
            candidates=matches,
            question=_city_question(value, matches),
        )
    return PointResolution(status="passthrough", value=value)


def _resolve_pair(
    origin: str,
    destination: str,
    *,
    mode: str,
    catalog: Sequence[Station],
) -> tuple[PointResolution, PointResolution]:
    origin_exact = is_exact_point(origin)
    dest_exact = is_exact_point(destination)
    if not origin_exact and not dest_exact:
        documented = _find_documented_pair(origin, destination, mode=mode, catalog=catalog)
        if documented is not None:
            left, right = documented
            return (
                PointResolution(status="resolved", value=left.search_value, candidates=(left,)),
                PointResolution(status="resolved", value=right.search_value, candidates=(right,)),
            )
    origin_res = _resolve_one(origin, catalog=catalog)
    destination_res = _resolve_one(destination, catalog=catalog)
    # Mixed coded station + bare city is a live Tutu failure mode.  If only
    # one end could be resolved from instructions, keep the original cities.
    mixed = {origin_res.status, destination_res.status} == {"resolved", "passthrough"}
    if mixed:
        return (
            PointResolution(status="passthrough", value=origin),
            PointResolution(status="passthrough", value=destination),
        )
    return origin_res, destination_res


def _city_question(city: str, stations: Sequence[Station]) -> str:
    options = "; ".join(station.search_value for station in stations)
    return f"Несколько станций подходят для «{city}». Какая нужна: {options}?"


def _route_question(
    origin: str,
    destination: str,
    origin_res: PointResolution,
    destination_res: PointResolution,
    *,
    mode: str,
) -> str:
    mode_label = _MODE_LABELS.get(mode, mode)
    if origin_res.ambiguous and not destination_res.ambiguous:
        return origin_res.question or _city_question(origin, origin_res.candidates)
    if destination_res.ambiguous and not origin_res.ambiguous:
        return destination_res.question or _city_question(destination, destination_res.candidates)
    if origin_res.ambiguous and destination_res.ambiguous:
        origin_opts = "; ".join(station.search_value for station in origin_res.candidates) or origin
        dest_opts = "; ".join(station.search_value for station in destination_res.candidates) or destination
        return (
            f"Для {mode_label} несколько станций. Откуда: {origin_opts}. Куда: {dest_opts}?"
        )
    return f"Уточните станции для маршрута {origin} → {destination}."

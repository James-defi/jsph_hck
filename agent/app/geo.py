"""Bind globe pins to real IATA / station / city search results.

Frontend used to guess a city from route text, so
``Санкт-Петербург — Московский вокзал`` became Moscow via the substring
``москва``.  This module maps a single origin or destination value to
coordinates without substring city matching.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .station_resolve import _STATION_RE, normalize_city, point_tokens, same_city


_IATA_RE = re.compile(r"^[A-Za-z]{3}$")
_GEO_ID_RE = re.compile(r"^\d{4,}$")

_AIR_MODES = frozenset({"avia", "air", "flight", "plane", "airline", "самолёт", "самолет", "авиа"})
_RAIL_MODES = frozenset({"rail", "train", "etrain", "поезд", "жд", "railway"})
_BUS_MODES = frozenset({"bus", "coach", "автобус"})


@dataclass(frozen=True, slots=True)
class GlobePlace:
    code: str
    label: str
    lat: float
    lon: float

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "lat": self.lat, "lon": self.lon}


def _airport(code: str, city: str, lat: float, lon: float) -> GlobePlace:
    return GlobePlace(code=code, label=f"{city} · {code}", lat=lat, lon=lon)


def _city_pin(code: str, city: str, lat: float, lon: float) -> GlobePlace:
    return GlobePlace(code=code, label=city, lat=lat, lon=lon)


# Exact 3-letter lookup.  Metro codes (MOW/MSK/SPB) are city centres, not a
# default airport — VKO/SVO/DME/LED keep their own coordinates.
_AIRPORTS: dict[str, GlobePlace] = {
    "VKO": _airport("VKO", "Москва", 55.591, 37.261),
    "SVO": _airport("SVO", "Москва", 55.973, 37.415),
    "DME": _airport("DME", "Москва", 55.414, 37.906),
    "LED": _airport("LED", "Санкт-Петербург", 59.800, 30.263),
    "SVX": _airport("SVX", "Екатеринбург", 56.743, 60.803),
    "KZN": _airport("KZN", "Казань", 55.606, 49.277),
    "IST": _airport("IST", "Стамбул", 41.275, 28.752),
    "SAW": _airport("SAW", "Стамбул", 40.898, 29.309),
    "AYT": _airport("AYT", "Анталья", 36.898, 30.800),
    "LHR": _airport("LHR", "Лондон", 51.470, -0.454),
    "LGW": _airport("LGW", "Лондон", 51.153, -0.182),
    "STN": _airport("STN", "Лондон", 51.885, 0.235),
    "AER": _airport("AER", "Сочи", 43.450, 39.956),
    "OVB": _airport("OVB", "Новосибирск", 55.012, 82.650),
    "KRR": _airport("KRR", "Краснодар", 45.034, 39.170),
    "CDG": _airport("CDG", "Париж", 49.010, 2.548),
    "FRA": _airport("FRA", "Франкфурт", 50.037, 8.562),
    "AMS": _airport("AMS", "Амстердам", 52.308, 4.764),
    "CEK": _airport("CEK", "Челябинск", 55.306, 61.503),
    "UFA": _airport("UFA", "Уфа", 54.557, 55.874),
    "KUF": _airport("KUF", "Самара", 53.505, 50.164),
    "GOJ": _airport("GOJ", "Нижний Новгород", 56.230, 43.784),
    "PEE": _airport("PEE", "Пермь", 57.914, 56.021),
    "TJM": _airport("TJM", "Тюмень", 57.189, 65.324),
    "KGD": _airport("KGD", "Калининград", 54.890, 20.592),
    "MRV": _airport("MRV", "Минеральные Воды", 44.225, 43.082),
    "SGC": _airport("SGC", "Сургут", 61.344, 73.402),
    "MUC": _airport("MUC", "Мюнхен", 48.354, 11.786),
    "VIE": _airport("VIE", "Вена", 48.110, 16.570),
    "WAW": _airport("WAW", "Варшава", 52.166, 20.967),
    "PRG": _airport("PRG", "Прага", 50.101, 14.260),
    "HEL": _airport("HEL", "Хельсинки", 60.317, 24.963),
    "RIX": _airport("RIX", "Рига", 56.924, 23.971),
    "TLL": _airport("TLL", "Таллин", 59.413, 24.833),
    "MSQ": _airport("MSQ", "Минск", 53.882, 28.031),
    "FCO": _airport("FCO", "Рим", 41.800, 12.239),
    "BCN": _airport("BCN", "Барселона", 41.297, 2.078),
    "MAD": _airport("MAD", "Мадрид", 40.472, -3.563),
    "DXB": _airport("DXB", "Дубай", 25.253, 55.365),
    "AUH": _airport("AUH", "Абу-Даби", 24.433, 54.651),
    "JFK": _airport("JFK", "Нью-Йорк", 40.641, -73.778),
    "TBS": _airport("TBS", "Тбилиси", 41.669, 44.955),
    "EVN": _airport("EVN", "Ереван", 40.147, 44.396),
    "GYD": _airport("GYD", "Баку", 40.467, 50.047),
    "ALA": _airport("ALA", "Алматы", 43.352, 77.040),
    "TAS": _airport("TAS", "Ташкент", 41.257, 69.281),
    "SIP": _airport("SIP", "Симферополь", 45.052, 33.975),
    "ROV": _airport("ROV", "Ростов-на-Дону", 47.258, 39.818),
    "MOW": _city_pin("MOW", "Москва", 55.756, 37.617),
    "MSK": _city_pin("MSK", "Москва", 55.756, 37.617),
    "SPB": _city_pin("SPB", "Санкт-Петербург", 59.932, 30.308),
    "KLF": _city_pin("KLF", "Калуга", 54.514, 36.267),
    "VBG": _city_pin("VBG", "Выборг", 60.711, 28.749),
}

_STATION_IDS: dict[str, GlobePlace] = {
    "2006004": _city_pin("2006004", "Москва", 55.756, 37.617),
    "2004004": _city_pin("2004004", "Санкт-Петербург", 59.932, 30.308),
    "2004001": _city_pin("2004001", "Санкт-Петербург", 59.932, 30.308),
}


@dataclass(frozen=True, slots=True)
class _CitySpec:
    label: str
    aliases: frozenset[str]
    air_code: str
    center_code: str | None = None
    center: tuple[float, float] | None = None


_CITY_SPECS: tuple[_CitySpec, ...] = (
    _CitySpec(
        label="Москва",
        aliases=frozenset({"москва", "moscow", "mow", "msk"}),
        air_code="VKO",
        center_code="MOW",
        center=(55.756, 37.617),
    ),
    _CitySpec(
        label="Санкт-Петербург",
        aliases=frozenset(
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
        ),
        air_code="LED",
        center_code="SPB",
        center=(59.932, 30.308),
    ),
    _CitySpec(
        label="Казань",
        aliases=frozenset({"казань", "kazan", "kzn"}),
        air_code="KZN",
        center_code="KZN",
        center=(55.796, 49.106),
    ),
    _CitySpec(
        label="Екатеринбург",
        aliases=frozenset({"екатеринбург", "ekaterinburg", "yekaterinburg", "svx"}),
        air_code="SVX",
        center_code="SVX",
        center=(56.838, 60.597),
    ),
    _CitySpec(
        label="Калуга",
        aliases=frozenset({"калуга", "kaluga", "klf"}),
        air_code="KLF",
        center_code="KLF",
        center=(54.514, 36.267),
    ),
    _CitySpec(
        label="Выборг",
        aliases=frozenset({"выборг", "vyborg", "vbg"}),
        air_code="VBG",
        center_code="VBG",
        center=(60.711, 28.749),
    ),
    _CitySpec(label="Стамбул", aliases=frozenset({"стамбул", "istanbul"}), air_code="IST"),
    *(
        # Ground-only pins: Tutu sells rail and bus to many cities without an
        # airport, and the globe still needs their coordinates.
        _CitySpec(
            label=label,
            aliases=frozenset(aliases),
            air_code=air_code,
            center_code=center_code,
            center=(lat, lon),
        )
        for label, aliases, center_code, lat, lon, air_code in (
            ("Тверь", ("тверь", "tver"), "TVER", 56.859, 35.912, ""),
            ("Владимир", ("владимир", "vladimir"), "VLADIMIR", 56.129, 40.407, ""),
            ("Нижний Новгород", ("нижнийновгород", "нижний", "nizhnynovgorod", "goj"), "NN", 56.327, 44.006, "GOJ"),
            ("Великий Новгород", ("великийновгород", "новгород", "velikynovgorod"), "VNOV", 58.522, 31.270, ""),
            ("Псков", ("псков", "pskov"), "PSKOV", 57.819, 28.332, ""),
            ("Петрозаводск", ("петрозаводск", "petrozavodsk"), "PTZ", 61.789, 34.359, ""),
            ("Тула", ("тула", "tula"), "TULA", 54.193, 37.617, ""),
            ("Рязань", ("рязань", "ryazan"), "RYAZAN", 54.625, 39.736, ""),
            ("Ярославль", ("ярославль", "yaroslavl"), "YAR", 57.626, 39.894, ""),
            ("Вологда", ("вологда", "vologda"), "VOLOGDA", 59.220, 39.892, ""),
            ("Смоленск", ("смоленск", "smolensk"), "SMOLENSK", 54.782, 32.045, ""),
            ("Воронеж", ("воронеж", "voronezh"), "VRN", 51.661, 39.200, ""),
            ("Самара", ("самара", "samara", "kuf"), "SAMARA", 53.195, 50.101, "KUF"),
            ("Уфа", ("уфа", "ufa"), "UFACITY", 54.735, 55.958, "UFA"),
            ("Пермь", ("пермь", "perm"), "PERMCITY", 58.010, 56.229, "PEE"),
            ("Челябинск", ("челябинск", "chelyabinsk"), "CHEL", 55.160, 61.403, "CEK"),
            ("Ростов-на-Дону", ("ростовнадону", "ростов", "rostov"), "ROSTOV", 47.222, 39.719, "ROV"),
            ("Волгоград", ("волгоград", "volgograd"), "VOLGOGRAD", 48.708, 44.513, ""),
            ("Саратов", ("саратов", "saratov"), "SARATOV", 51.533, 46.034, ""),
            ("Киров", ("киров", "kirov"), "KIROV", 58.604, 49.668, ""),
            ("Ижевск", ("ижевск", "izhevsk"), "IZHEVSK", 56.852, 53.211, ""),
            ("Чебоксары", ("чебоксары", "cheboksary"), "CHEB", 56.146, 47.251, ""),
            ("Йошкар-Ола", ("йошкарола", "yoshkarola"), "YOLA", 56.634, 47.899, ""),
            ("Пенза", ("пенза", "penza"), "PENZA", 53.195, 45.005, ""),
            ("Ульяновск", ("ульяновск", "ulyanovsk"), "ULY", 54.317, 48.403, ""),
            ("Мурманск", ("мурманск", "murmansk"), "MURMANSK", 68.971, 33.075, ""),
            ("Архангельск", ("архангельск", "arkhangelsk"), "ARH", 64.539, 40.518, ""),
            ("Курск", ("курск", "kursk"), "KURSK", 51.731, 36.193, ""),
            ("Белгород", ("белгород", "belgorod"), "BELGOROD", 50.596, 36.588, ""),
            ("Брянск", ("брянск", "bryansk"), "BRYANSK", 53.243, 34.364, ""),
            ("Иваново", ("иваново", "ivanovo"), "IVANOVO", 57.000, 40.974, ""),
            ("Кострома", ("кострома", "kostroma"), "KOSTROMA", 57.768, 40.927, ""),
            ("Тюмень", ("тюмень", "tyumen"), "TYUMEN", 57.153, 65.534, "TJM"),
            ("Калининград", ("калининград", "kaliningrad"), "KLGD", 54.710, 20.511, "KGD"),
            ("Сергиев Посад", ("сергиевпосад", "sergievposad"), "SPOSAD", 56.315, 38.136, ""),
            ("Петергоф", ("петергоф", "peterhof"), "PETERHOF", 59.884, 29.908, ""),
        )
    ),
    _CitySpec(label="Лондон", aliases=frozenset({"лондон", "london"}), air_code="LHR"),
    _CitySpec(label="Сочи", aliases=frozenset({"сочи", "sochi"}), air_code="AER"),
    _CitySpec(label="Новосибирск", aliases=frozenset({"новосибирск", "novosibirsk"}), air_code="OVB"),
    _CitySpec(label="Краснодар", aliases=frozenset({"краснодар", "krasnodar"}), air_code="KRR"),
    _CitySpec(label="Париж", aliases=frozenset({"париж", "paris"}), air_code="CDG"),
    _CitySpec(label="Франкфурт", aliases=frozenset({"франкфурт", "frankfurt"}), air_code="FRA"),
    _CitySpec(label="Амстердам", aliases=frozenset({"амстердам", "amsterdam"}), air_code="AMS"),
)

_CITY_BY_ALIAS: dict[str, _CitySpec] = {
    alias: spec for spec in _CITY_SPECS for alias in spec.aliases
}


def _normalize_mode(mode: str) -> str:
    text = str(mode or "").strip().lower()
    if text in _RAIL_MODES:
        return "rail"
    if text in _BUS_MODES:
        return "bus"
    if text in _AIR_MODES:
        return "air"
    return "air"


def _place_from_code(code: str) -> GlobePlace | None:
    token = str(code or "").strip().upper()
    if not token:
        return None
    if token in _STATION_IDS:
        return _STATION_IDS[token]
    if _IATA_RE.fullmatch(token):
        return _AIRPORTS.get(token)
    return None


def _place_from_city_spec(spec: _CitySpec, *, mode: str) -> GlobePlace | None:
    if mode in {"rail", "bus"} and spec.center is not None and spec.center_code:
        return _city_pin(spec.center_code, spec.label, spec.center[0], spec.center[1])
    return _AIRPORTS.get(spec.air_code)


def _resolve_city_token(token: str, *, mode: str) -> GlobePlace | None:
    if not token:
        return None
    spec = _CITY_BY_ALIAS.get(token)
    if spec is None:
        return None
    return _place_from_city_spec(spec, mode=mode)


def _resolve_station_match(match: re.Match[str], *, mode: str) -> GlobePlace | None:
    code = str(match.group("code") or "").strip().upper()
    city_text = str(match.group("city") or "").strip()
    by_code = _place_from_code(code)
    if by_code is not None:
        return by_code
    return _resolve_city_token(normalize_city(city_text), mode=mode)


def resolve_place(value: Any, *, mode: str = "air") -> GlobePlace | None:
    """Map one search result / station / IATA / city name to globe coordinates."""

    if isinstance(value, GlobePlace):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    family = _normalize_mode(mode)

    station_match = _STATION_RE.search(text)
    if station_match is not None:
        resolved = _resolve_station_match(station_match, mode=family)
        if resolved is not None:
            return resolved

    if _IATA_RE.fullmatch(text):
        place = _AIRPORTS.get(text.upper())
        if place is not None:
            return place

    if _GEO_ID_RE.fullmatch(text):
        place = _STATION_IDS.get(text)
        if place is not None:
            return place

    place = _resolve_city_token(normalize_city(text), mode=family)
    if place is not None:
        return place
    return _resolve_city_token(_city_prefix(text), mode=family)


def _city_prefix(text: str) -> str:
    """City part of a Tutu endpoint such as ``Выборг, 2004682``.

    Only separators that never occur inside a city name are used, so
    ``Санкт-Петербург — Ладожский вокзал`` still yields the city and never the
    station word.
    """

    head = re.split(r"[,—–(]", text, maxsplit=1)[0]
    return normalize_city(head)


def _iata_token(value: str) -> str | None:
    text = str(value or "").strip().upper()
    if _IATA_RE.fullmatch(text):
        return text
    match = re.search(r"\(([A-Z]{3})\)", text)
    return match.group(1) if match else None


def meeting_points_compatible(requested: str, observed: str, *, mode: str = "air") -> bool:
    """True when a contract point and an offer point are the same meeting place.

    Distinct IATA codes stay distinct (IST is not SAW).  Rail and bus city
    names may match a station id or stop in the same city.
    """

    left = str(requested or "").strip()
    right = str(observed or "").strip()
    if not left or not right:
        return False
    if left.upper() == right.upper():
        return True
    left_iata = _iata_token(left)
    right_iata = _iata_token(right)
    if left_iata and right_iata:
        return left_iata == right_iata
    if point_tokens(left) & point_tokens(right):
        return True
    if same_city(left, right):
        return True
    origin = resolve_place(left, mode=mode)
    destination = resolve_place(right, mode=mode)
    if origin is None or destination is None:
        return False
    return abs(origin.lat - destination.lat) < 0.35 and abs(origin.lon - destination.lon) < 0.35


def meeting_point_matches(
    requested: str,
    *,
    code: str | None = None,
    label: str | None = None,
    mode: str = "air",
) -> bool:
    """Compare a contract point with an offer endpoint given by code and name.

    Tutu station ids are unbounded, so an unknown id such as ``2004682`` cannot
    be resolved from a static table.  For rail and bus the offer also carries a
    human endpoint (``Выборг, 2004682``), and that name is authoritative for the
    meeting city.  Aviation keeps the strict code comparison.
    """

    if meeting_points_compatible(requested, code or "", mode=mode):
        return True
    if not label or _normalize_mode(mode) not in {"rail", "bus"}:
        return False
    return meeting_points_compatible(requested, label, mode=mode)

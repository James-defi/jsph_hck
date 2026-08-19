"""City → station resolution from Tutu instruction text, without invented geo ids."""

from __future__ import annotations

from agent.app.station_resolve import (
    extract_stations,
    is_exact_point,
    points_compatible,
    resolve_route,
    same_city,
)


RAIL_DEFAULT_INSTRUCTIONS = {
    "text": (
        "Для search_rail указывай станции с кодами, например:\n"
        'origin="Москва — Ленинградский вокзал (2006004)"\n'
        'destination="Санкт-Петербург — Московский вокзал (2004004)"\n'
    )
}

AMBIGUOUS_MOSCOW_INSTRUCTIONS = {
    "text": (
        "Москва — Казанский вокзал (2000001)\n"
        "Москва — Ярославский вокзал (2000003)\n"
        "Казань — Вокзал (2060001)\n"
    )
}

EXTRA_MOSCOW_WITH_DEFAULT_PAIR = {
    "text": (
        "Москва — Казанский вокзал (2000001)\n"
        "Москва — Ленинградский вокзал (2006004)\n"
        "Санкт-Петербург — Московский вокзал (2004004)\n"
    )
}


def test_extract_stations_reads_coded_examples_from_instruction_text() -> None:
    stations = extract_stations(RAIL_DEFAULT_INSTRUCTIONS)
    codes = {station.code for station in stations}
    assert codes == {"2006004", "2004004"}
    by_code = {station.code: station.search_value for station in stations}
    assert "Ленинградский" in by_code["2006004"]
    assert "Московский" in by_code["2004004"]
    spb = next(station for station in stations if station.code == "2004004")
    assert "Санкт-Петербург" in spb.city
    assert "Московский" in spb.name


def test_moscow_spb_uses_documented_pair_from_instructions() -> None:
    route = resolve_route(
        "Москва",
        "Санкт-Петербург",
        mode="rail",
        instructions=RAIL_DEFAULT_INSTRUCTIONS,
    )
    assert not route.ambiguous
    assert "2006004" in route.origin.value
    assert "2004004" in route.destination.value
    assert route.origin.status == "resolved"
    assert route.destination.status == "resolved"


def test_documented_pair_wins_over_extra_moscow_station() -> None:
    route = resolve_route(
        "Москва",
        "Санкт-Петербург",
        mode="rail",
        instructions=EXTRA_MOSCOW_WITH_DEFAULT_PAIR,
    )
    assert not route.ambiguous
    assert "2006004" in route.origin.value
    assert "Казанский" not in route.origin.value


def test_two_moscow_stations_without_default_pair_are_ambiguous() -> None:
    route = resolve_route(
        "Москва",
        "Казань",
        mode="rail",
        instructions=AMBIGUOUS_MOSCOW_INSTRUCTIONS,
    )
    assert route.ambiguous
    assert route.question
    assert "Казанский" in (route.question or "")
    assert "Ярославский" in (route.question or "")


def test_empty_instructions_do_not_invent_geo_ids() -> None:
    route = resolve_route(
        "Москва",
        "Санкт-Петербург",
        mode="rail",
        instructions={"text": "rail search instructions"},
    )
    assert not route.ambiguous
    assert route.origin.status == "passthrough"
    assert route.origin.value == "Москва"
    assert route.destination.value == "Санкт-Петербург"
    assert "2006004" not in route.origin.value
    assert "2004004" not in route.destination.value


def test_one_sided_catalog_does_not_mix_coded_origin_with_city_destination() -> None:
    route = resolve_route(
        "Москва",
        "Санкт-Петербург",
        mode="rail",
        instructions={"text": "Москва — Ленинградский вокзал (2006004)"},
    )
    assert not route.ambiguous
    assert route.origin.value == "Москва"
    assert route.destination.value == "Санкт-Петербург"


def test_iata_and_coded_station_are_exact() -> None:
    assert is_exact_point("VKO")
    assert is_exact_point("2006004")
    assert is_exact_point("Москва — Ленинградский вокзал (2006004)")
    assert not is_exact_point("Москва")
    assert not is_exact_point("Санкт-Петербург")


def test_resolved_station_is_compatible_with_numeric_offer_code() -> None:
    assert points_compatible("Москва — Ленинградский вокзал (2006004)", "2006004")
    assert points_compatible("VKO", "VKO")
    assert points_compatible("Москва", "2006004")


def test_same_city_matches_aliases_and_coded_station_strings() -> None:
    assert same_city("Казань", "Казань — Автовокзал Южный (1658001)")
    assert same_city("MOW", "Москва")
    assert not same_city("Казань", "Москва")


def test_unique_city_station_resolves_without_documented_pair() -> None:
    route = resolve_route(
        "Казань",
        "Владивосток",
        mode="rail",
        instructions={"text": "Казань — Вокзал (2060001)\nВладивосток — Вокзал (2034000)"},
    )
    assert not route.ambiguous
    assert route.origin.value.endswith("(2060001)")
    assert route.destination.value.endswith("(2034000)")


def test_bus_default_pair_uses_named_stops_from_instructions() -> None:
    route = resolve_route(
        "Казань",
        "Москва",
        mode="bus",
        instructions={
            "text": (
                "Казань — Автовокзал Южный (1658001)\n"
                "Москва — Центральный автовокзал (1111111)\n"
                "Москва — Международный автовокзал Саларьево (2222222)\n"
            )
        },
    )
    assert not route.ambiguous
    assert "Южный" in route.origin.value
    assert "Саларьево" in route.destination.value
    assert "1658001" in route.origin.value
    assert "2222222" in route.destination.value

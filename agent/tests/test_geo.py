"""Globe geo: IATA, station ids and city aliases without substring false matches."""

from __future__ import annotations

import pytest

from agent.app.geo import GlobePlace, meeting_point_matches, resolve_place
from agent.app.presentation import build_individual_presentation
from agent.app.web import _demo_payload


def test_svo_and_led_are_different_cities() -> None:
    svo = resolve_place("SVO")
    led = resolve_place("LED")
    assert svo is not None
    assert led is not None
    assert svo.lat == pytest.approx(55.973, abs=0.05)
    assert led.lat == pytest.approx(59.8, abs=0.05)
    assert svo.lat != led.lat
    assert svo.to_dict()["code"] == "SVO"
    assert led.to_dict()["code"] == "LED"


def test_spb_moskovsky_station_is_petersburg_not_moscow() -> None:
    place = resolve_place("Санкт-Петербург — Московский вокзал (2004004)", mode="rail")
    assert place is not None
    assert place.lat > 59
    moscow = resolve_place("Москва", mode="rail")
    assert moscow is not None
    assert place.lat != moscow.lat
    assert place.lat == pytest.approx(59.932, abs=0.05)


def test_moscow_leningradsky_station_is_moscow() -> None:
    place = resolve_place("Москва — Ленинградский вокзал (2006004)", mode="rail")
    assert place is not None
    assert place.lat == pytest.approx(55.7, abs=0.2)
    assert place.lat < 57


def test_kazan_and_moscow_city_names_differ() -> None:
    kazan = resolve_place("Казань")
    moscow = resolve_place("Москва")
    assert kazan is not None
    assert moscow is not None
    assert (kazan.lat, kazan.lon) != (moscow.lat, moscow.lon)


def test_meeting_points_match_rail_city_to_station_id_but_not_foreign_iata() -> None:
    from agent.app.geo import meeting_points_compatible

    assert meeting_points_compatible("Санкт-Петербург", "2004004", mode="rail")
    assert meeting_points_compatible("Выборг", "VBG", mode="rail")
    assert not meeting_points_compatible("IST", "SAW", mode="avia")
    assert not meeting_points_compatible("VKO", "LED", mode="avia")


def test_individual_timeline_leg_has_vko_led_coords() -> None:
    offer = {
        "id": "SU100",
        "mode": "avia",
        "segments": [
            {
                "origin_code": "VKO",
                "destination_code": "LED",
                "departure_at": "2026-09-25T08:10:00+03:00",
                "arrival_at": "2026-09-25T09:40:00+03:00",
            }
        ],
        "price": {"amount": 7400, "currency": "RUB"},
    }
    presentation, _components = build_individual_presentation(
        origin="VKO",
        destination="LED",
        mode="avia",
        departure_date="2026-09-25",
        adults=1,
        min_wait_minutes=0,
        max_wait_minutes=240,
        items=[{"offer": offer, "raw_offer": offer}],
        query="VKO → LED",
    )
    legs = presentation["timeline"][0]["legs"]
    assert len(legs) == 1
    origin = legs[0]["origin"]
    destination = legs[0]["destination"]
    assert origin["code"] == "VKO"
    assert origin["lat"] == pytest.approx(55.591, abs=0.02)
    assert destination["code"] == "LED"
    assert destination["lat"] == pytest.approx(59.800, abs=0.02)
    assert set(origin) == {"code", "label", "lat", "lon"}
    assert set(destination) == {"code", "label", "lat", "lon"}
    assert "checkout_ref" not in origin
    assert origin["label"] == "Москва · VKO"
    assert destination["label"] == "Санкт-Петербург · LED"


def test_globe_place_json_shape() -> None:
    payload = GlobePlace(code="SVO", label="Москва · SVO", lat=55.973, lon=37.415).to_dict()
    assert payload == {"code": "SVO", "label": "Москва · SVO", "lat": 55.973, "lon": 37.415}


def test_station_name_moskovsky_is_not_a_city_alias() -> None:
    assert resolve_place("Московский", mode="rail") is None
    assert resolve_place("Московский вокзал", mode="rail") is None


def test_unknown_rail_station_id_uses_city_from_full_name() -> None:
    place = resolve_place("Санкт-Петербург — Московский вокзал (2004999)", mode="rail")
    assert place is not None
    assert place.lat == pytest.approx(59.932, abs=0.05)
    assert resolve_place("2004001", mode="rail") is not None


def test_city_comma_station_id_resolves_to_the_city() -> None:
    """Tutu returns endpoints such as ``Выборг, 2004682`` with unknown ids."""

    vyborg = resolve_place("Выборг, 2004682", mode="rail")
    tver = resolve_place("Тверь, 2004600", mode="rail")
    assert vyborg is not None and vyborg.lat == pytest.approx(60.711, abs=0.05)
    assert tver is not None and tver.lat == pytest.approx(56.859, abs=0.05)


def test_meeting_point_matches_station_label_but_keeps_iata_strict() -> None:
    assert meeting_point_matches("Выборг", code="2004682", label="Выборг, 2004682", mode="rail")
    assert not meeting_point_matches("Выборг", code="2004682", mode="rail")
    # An aviation contract never accepts a second airport of the same city.
    assert not meeting_point_matches("SAW", code="IST", label="Стамбул (IST)", mode="avia")


def test_rail_timeline_uses_station_names_not_bare_ids() -> None:
    offer = {
        "id": "RZD742",
        "mode": "rail",
        "segments": [
            {
                "origin_code": "2006004",
                "destination_code": "2004999",
                "origin_name": "Москва — Ленинградский вокзал (2006004)",
                "destination_name": "Санкт-Петербург — Московский вокзал (2004999)",
                "departure_at": "2026-09-10T06:00:00+03:00",
                "arrival_at": "2026-09-10T12:13:00+03:00",
            }
        ],
        "price": {"amount": 1623, "currency": "RUB"},
        "checkout_ref": {"offer_hash": "rail-hash", "train_number": "742"},
    }
    presentation, components = build_individual_presentation(
        origin="Москва",
        destination="Санкт-Петербург",
        mode="rail",
        departure_date="2026-09-10",
        adults=1,
        min_wait_minutes=0,
        max_wait_minutes=1440,
        items=[{"offer": offer, "raw_offer": offer}],
        query="Москва → Санкт-Петербург поездом",
    )
    leg = presentation["timeline"][0]["legs"][0]
    assert "Москва" in leg["route"]
    assert "Санкт-Петербург" in leg["route"]
    assert "2004999" not in leg["route"]
    assert leg["origin"]["lat"] == pytest.approx(55.756, abs=0.05)
    assert leg["destination"]["lat"] == pytest.approx(59.932, abs=0.05)
    tariffs = presentation["scenarios"][0]["booking_units"][0]["tariffs"]
    assert tariffs
    assert components
    payload = _demo_payload(run_id="demo-geo", query="IST")
    anya, ilya, sasha = payload["timeline"]
    assert anya["legs"][0]["origin"]["code"] == "VKO"
    assert anya["legs"][0]["origin"]["lat"] == pytest.approx(55.591, abs=0.01)
    assert ilya["legs"][0]["origin"]["code"] == "LED"
    assert ilya["legs"][0]["origin"]["lat"] == pytest.approx(59.800, abs=0.01)
    assert sasha["legs"][0]["origin"]["code"] == "SVX"
    assert sasha["legs"][0]["origin"]["lat"] == pytest.approx(56.743, abs=0.01)
    assert anya["legs"][0]["origin"]["lat"] != ilya["legs"][0]["origin"]["lat"]
    for traveler in payload["timeline"]:
        for leg in traveler["legs"]:
            assert set(leg["origin"]) == {"code", "label", "lat", "lon"}
            assert set(leg["destination"]) == {"code", "label", "lat", "lon"}


def test_bus_timeline_keeps_city_words_coords_and_parent_checkout_ref() -> None:
    offer = {
        "id": "BUS11",
        "mode": "bus",
        "transport": "bus",
        "segments": [
            {
                "origin_code": "Автовокзал Восточный",
                "destination_code": "Автовокзал Котельники",
                "origin_name": "Автовокзал Восточный",
                "destination_name": "Автовокзал Котельники",
                "from": "Автовокзал Восточный",
                "to": "Автовокзал Котельники",
                "departure_at": "2026-09-10T20:00:00+03:00",
                "arrival_at": "2026-09-11T11:00:00+03:00",
            }
        ],
        "price": {"amount": 2100, "currency": "RUB"},
        "checkout_ref": {
            "offer_hash": "bus-hash",
            "passengers_adult": 1,
            "city_from": "Казань",
            "city_to": "Москва",
        },
        "variants": [{"variant_id": "default", "price": {"amount": 2100, "currency": "RUB"}}],
        "meta": {"from": "Казань", "to": "Москва"},
    }
    presentation, components = build_individual_presentation(
        origin="Казань",
        destination="Москва",
        mode="bus",
        departure_date="2026-09-10",
        adults=1,
        min_wait_minutes=0,
        max_wait_minutes=1440,
        items=[{"offer": offer, "raw_offer": offer, "selected_checkout_ref": offer["checkout_ref"]}],
        query="Автобус Казань Москва",
    )
    leg = presentation["timeline"][0]["legs"][0]
    assert "Казань" in leg["route"]
    assert "Москва" in leg["route"]
    assert "Восточный" in leg["route"]
    assert "Котельники" in leg["route"]
    assert leg["origin"]["lat"] == pytest.approx(55.796, abs=0.05)
    assert leg["destination"]["lat"] == pytest.approx(55.756, abs=0.05)
    tariffs = presentation["scenarios"][0]["booking_units"][0]["tariffs"]
    assert tariffs
    assert components

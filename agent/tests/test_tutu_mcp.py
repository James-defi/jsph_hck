"""Gateway argument normalization for live Tutu MCP schemas."""

from __future__ import annotations

import pytest

from agent.app.tutu_mcp import FakeTutuMcpClient, TutuMcpGateway, normalize_tutu_tool_arguments


def _gateway(responses: dict | None = None) -> tuple[TutuMcpGateway, FakeTutuMcpClient]:
    fake = FakeTutuMcpClient(responses or {})
    return TutuMcpGateway(fake), fake


def test_search_rail_coerces_passengers_dict_to_int() -> None:
    gateway, fake = _gateway({"search_rail": {"offers": []}})

    gateway.search_rail(
        {
            "origin": "Москва — Ленинградский вокзал (2006004)",
            "destination": "Санкт-Петербург — Московский вокзал (2004004)",
            "departure_date": "2026-09-25",
            "passengers": {"adults": 1, "children": 0, "infants": 0},
        }
    )

    assert fake.calls == [
        (
            "search_rail",
            {
                "origin": "Москва — Ленинградский вокзал (2006004)",
                "destination": "Санкт-Петербург — Московский вокзал (2004004)",
                "departure_date": "2026-09-25",
                "passengers": 1,
            },
        )
    ]


def test_search_rail_maps_adults_alias_and_drops_it() -> None:
    arguments = normalize_tutu_tool_arguments(
        "search_rail",
        {
            "origin": "Москва",
            "destination": "Санкт-Петербург",
            "departure_date": "2026-09-25",
            "adults": 1,
            "view": "compact",
        },
    )
    assert arguments["passengers"] == 1
    assert "adults" not in arguments


def test_search_bus_maps_from_to_and_drops_query() -> None:
    gateway, fake = _gateway({"search_bus": {"offers": []}})

    gateway.search_bus(
        {
            "from": "Казань",
            "to": "Москва",
            "departure_date": "2026-09-25",
            "adults": 1,
            "query": "Казань Москва 2026-09-25 1 взрослый",
        }
    )

    name, arguments = fake.calls[0]
    assert name == "search_bus"
    assert arguments["origin"] == "Казань"
    assert arguments["destination"] == "Москва"
    assert arguments["departure_date"] == "2026-09-25"
    assert arguments["adults"] == 1
    assert "from" not in arguments
    assert "to" not in arguments
    assert "query" not in arguments


def test_empty_get_offer_details_fails_locally_without_mcp() -> None:
    gateway, fake = _gateway({"get_offer_details": {"summary": "should not be called"}})

    with pytest.raises(ValueError, match="product_type") as exc_info:
        gateway.get_offer_details({})

    assert fake.calls == []
    message = str(exc_info.value)
    assert "pydantic.dev" not in message
    assert "https://" not in message


def test_get_offer_details_forwards_required_fields_and_drops_extras() -> None:
    gateway, fake = _gateway({"get_offer_details": {"summary": "ok"}})
    details_ref = {"hash": "opaque-details"}

    gateway.get_offer_details(
        {
            "product_type": "rail",
            "details_ref": details_ref,
            "query": "ignored",
            "from": "Москва",
        }
    )

    assert fake.calls == [
        ("get_offer_details", {"product_type": "rail", "details_ref": details_ref})
    ]


def test_search_bus_coerces_passengers_dict_to_adults() -> None:
    arguments = normalize_tutu_tool_arguments(
        "search_bus",
        {
            "origin": "Казань",
            "destination": "Москва",
            "departure_date": "2026-09-25",
            "passengers": {"adults": 2, "children": 1, "infants": 0},
        },
    )
    assert arguments["adults"] == 2
    assert "passengers" not in arguments
    assert "children" not in arguments
    assert "infants" not in arguments


def test_existing_origin_is_not_overwritten_by_from() -> None:
    arguments = normalize_tutu_tool_arguments(
        "search_avia",
        {"origin": "VKO", "from": "MOW", "destination": "LED", "to": "SPB", "adults": 1},
    )
    assert arguments["origin"] == "VKO"
    assert arguments["destination"] == "LED"
    assert "from" not in arguments
    assert "to" not in arguments


def test_create_checkout_link_forwards_ref_values_and_drops_non_schema_keys() -> None:
    # Live Tutu rejects checkout_ref keys outside its schema (extra_forbidden,
    # verified 2026-08-19); values for allowed keys ride byte-for-byte.
    checkout_ref = {
        "transport": "avia",
        "offer_hash": "opaque-offer",
        "passengers_full": 2,
        "service_class": "ECONOMIC",
        "is_multi_pnr": False,
        "query": "must-not-reach-tutu",
    }
    gateway, fake = _gateway({"create_checkout_link": {"checkout_url": "https://www.tutu.ru/checkout"}})

    gateway.create_checkout_link(checkout_ref)

    assert fake.calls == [
        (
            "create_checkout_link",
            {
                "transport": "avia",
                "offer_hash": "opaque-offer",
                "passengers_full": 2,
                "service_class": "ECONOMIC",
            },
        )
    ]

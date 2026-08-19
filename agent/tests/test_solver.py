"""Offline tests for GroupSync facts and guarded checkout handoff."""

from __future__ import annotations

import asyncio
import json as json_module

import pytest

from agent.app.service import CheckoutSelectionError, GroupSyncService
from agent.app.solver import (
    STATUS_RISK,
    STATUS_UNKNOWN,
    checkout_handoff_guard,
    expand_offer_variants,
    inspect_connection_risks,
    inspect_offer_risks,
    solve_group_rendezvous,
)
from agent.app.tutu_mcp import FakeTutuMcpClient, StreamableHttpMcpClient, TutuMcpGateway


def offer(
    identifier: str,
    *,
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    price: int = 10_000,
    carrier: str = "TK",
    number: str = "TK100",
    multi_pnr: bool | None = False,
    baggage: object | None = {"included": True, "pieces": 1},
    segments: list[dict] | None = None,
    passengers_full: int = 1,
) -> dict:
    raw = {
        "id": identifier,
        "mode": "avia",
        "price": {"amount": price, "currency": "RUB"},
        "legs": [
            {
                "segments": segments
                or [
                    {
                        "origin_code": origin,
                        "destination_code": destination,
                        "departure_at": departure,
                        "arrival_at": arrival,
                        "carrier": carrier,
                        "flight_number": number,
                    }
                ]
            }
        ],
        "checkout_ref": {
            "offer_hash": identifier,
            "passengers_full": passengers_full,
            "passengers_child": 0,
            "passengers_infant": 0,
        },
    }
    if multi_pnr is not None:
        raw["is_multi_pnr"] = multi_pnr
    if baggage is not None:
        raw["conditions"] = {"baggage": baggage}
    return raw


def contract(*, strict_baggage: bool = False) -> dict:
    return {
        "participants": [
            {"id": "moscow", "origin": "VKO"},
            {"id": "petersburg", "origin": "LED"},
        ],
        "hub_code": "IST",
        "destination_code": "LHR",
        "min_wait_minutes": 120,
        "max_wait_minutes": 300,
        "required_checked_baggage_pieces": 1,
        "strict_baggage": strict_baggage,
    }


def group_inputs() -> tuple[dict, list[dict], dict[str, list[dict]]]:
    common = offer(
        "common-tk1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        price=30_000,
        number="TK1983",
        passengers_full=2,
    )
    moscow = offer(
        "moscow-ist",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:10:00+03:00",
        arrival="2026-08-21T10:10:00+03:00",
        price=11_000,
        number="TK101",
    )
    petersburg = offer(
        "petersburg-ist",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T09:45:00+03:00",
        price=12_000,
        number="TK102",
    )
    return contract(), [common], {"moscow": [moscow], "petersburg": [petersburg]}


def test_solver_requires_exact_hub_window_and_shared_signature() -> None:
    request, common, feeders = group_inputs()

    result = solve_group_rendezvous(request, common, feeders)

    assert len(result["scenarios"]) == 1
    scenario = result["scenarios"][0]
    assert scenario["common_service_signature"] == {
        "mode": "avia",
        "carrier": "TK",
        "service_number": "TK1983",
        "origin_code": "IST",
        "destination_code": "LHR",
        "departure_at": "2026-08-21T13:30:00+03:00",
        "arrival_at": "2026-08-21T16:05:00+01:00",
    }
    waits = {item["participant_id"]: item["wait_minutes"] for item in scenario["feeders"]}
    assert waits == {"moscow": 200, "petersburg": 225}
    assert scenario["metrics"]["total_price"] == 53_000


def test_solver_rejects_saw_when_contract_requires_exact_ist() -> None:
    request, common, feeders = group_inputs()
    feeders["petersburg"] = [
        offer(
            "petersburg-saw",
            origin="LED",
            destination="SAW",
            departure="2026-08-21T06:00:00+03:00",
            arrival="2026-08-21T09:45:00+03:00",
        )
    ]

    result = solve_group_rendezvous(request, common, feeders)

    assert result["scenarios"] == []
    assert result["excluded_summary"]["different_hub_airport_or_station"] == 1


def test_solver_hides_feed_that_is_outside_min_max_window() -> None:
    request, common, feeders = group_inputs()
    # 60 minutes until the shared departure; contract requires at least 120.
    feeders["moscow"] = [
        offer(
            "too-short",
            origin="VKO",
            destination="IST",
            departure="2026-08-21T08:00:00+03:00",
            arrival="2026-08-21T12:30:00+03:00",
        )
    ]

    result = solve_group_rendezvous(request, common, feeders)

    assert result["scenarios"] == []
    assert result["excluded_summary"]["outside_meeting_window"] == 1


def test_solver_rejects_a_common_ticket_with_extra_segments_before_or_after_hub_leg() -> None:
    request, common, feeders = group_inputs()
    # The ticket contains IST → LHR, but actually starts in VKO.  It is not a
    # group component purchasable after independently meeting in IST.
    common[0]["legs"][0]["segments"].insert(
        0,
        {
            "origin_code": "VKO",
            "destination_code": "IST",
            "departure_at": "2026-08-21T08:00:00+03:00",
            "arrival_at": "2026-08-21T12:00:00+03:00",
            "carrier": "TK",
            "flight_number": "TK999",
        },
    )

    result = solve_group_rendezvous(request, common, feeders)

    assert result["scenarios"] == []
    assert result["excluded_summary"]["common_leg_not_exact_hub_to_destination"] == 1


def test_solver_matches_rail_city_hub_to_station_id() -> None:
    request = {
        "participants": [
            {"id": "moscow", "origin": "Москва"},
            {"id": "kazan", "origin": "Казань"},
        ],
        "hub_code": "Санкт-Петербург",
        "destination_code": "Выборг",
        "min_wait_minutes": 60,
        "max_wait_minutes": 240,
        "required_checked_baggage_pieces": 0,
        "strict_baggage": False,
    }
    common = offer(
        "spb-vyborg",
        origin="2004004",
        destination="VBG",
        departure="2026-09-10T16:00:00+03:00",
        arrival="2026-09-10T18:10:00+03:00",
        price=900,
        carrier="RZD",
        number="800A",
        passengers_full=2,
    )
    common["mode"] = "rail"
    moscow = offer(
        "msk-spb",
        origin="2006004",
        destination="2004004",
        departure="2026-09-10T06:00:00+03:00",
        arrival="2026-09-10T12:13:00+03:00",
        price=1600,
        carrier="RZD",
        number="742",
    )
    moscow["mode"] = "rail"
    kazan = offer(
        "kzn-spb",
        origin="KZN",
        destination="2004004",
        departure="2026-09-10T00:00:00+03:00",
        arrival="2026-09-10T14:00:00+03:00",
        price=2100,
        carrier="RZD",
        number="23",
    )
    kazan["mode"] = "rail"

    result = solve_group_rendezvous(request, [common], {"moscow": [moscow], "kazan": [kazan]})
    assert result["scenarios"], result.get("excluded_summary")
    signature = result["scenarios"][0]["common_service_signature"]
    assert signature["origin_code"] == "2004004"
    assert signature["destination_code"] == "VBG"


def test_risk_inspection_keeps_unknown_baggage_unknown_and_marks_real_risks() -> None:
    night_segments = [
        {
            "origin_code": "VKO",
            "destination_code": "IST",
            "departure_at": "2026-08-21T20:00:00+03:00",
            "arrival_at": "2026-08-22T01:00:00+03:00",
            "carrier": "TK",
            "flight_number": "TK1",
        },
        {
            "origin_code": "IST",
            "destination_code": "LHR",
            "departure_at": "2026-08-22T03:00:00+03:00",
            "arrival_at": "2026-08-22T06:00:00+01:00",
            "carrier": "TK",
            "flight_number": "TK2",
        },
    ]
    risky = offer(
        "self-transfer-night",
        origin="VKO",
        destination="LHR",
        departure="2026-08-21T20:00:00+03:00",
        arrival="2026-08-22T06:00:00+01:00",
        multi_pnr=True,
        baggage=None,
        segments=night_segments,
    )

    inspected = inspect_offer_risks(risky, required_checked_baggage_pieces=1)
    statuses = {item["code"]: item["status"] for item in inspected["findings"]}
    assert inspected["overall_status"] == STATUS_RISK
    assert statuses["self_transfer"] == STATUS_RISK
    assert statuses["night_connection"] == STATUS_RISK
    assert statuses["checked_baggage"] == STATUS_UNKNOWN


def test_separate_checkouts_do_not_prove_through_baggage_even_when_both_flags_are_true() -> None:
    feeder = offer(
        "feeder",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T10:30:00+03:00",
    )
    common = offer(
        "common",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
    )
    feeder["conditions"]["through_checked_baggage"] = True
    common["conditions"]["through_checked_baggage"] = True

    result = inspect_connection_risks(feeder, common, required_checked_baggage_pieces=1)
    by_code = {item["code"]: item for item in result["findings"]}
    assert by_code["through_checked_baggage"]["status"] == STATUS_UNKNOWN
    assert by_code["through_checked_baggage"]["evidence"]["same_carrier"] is True
    assert "спросите на регистрации" in by_code["through_checked_baggage"]["message"]

    jointly_confirmed = inspect_connection_risks(
        feeder,
        common,
        required_checked_baggage_pieces=1,
        joint_through_checked_baggage=True,
    )
    joint_by_code = {item["code"]: item for item in jointly_confirmed["findings"]}
    assert joint_by_code["through_checked_baggage"]["status"] == "pass"


def test_short_user_permitted_window_is_a_risk_not_a_hidden_recommendation() -> None:
    """Hard filters and structural risk are intentionally separate layers."""

    feeder = offer(
        "feeder",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T11:30:00+03:00",
    )
    common = offer(
        "common",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
    )

    result = inspect_connection_risks(feeder, common)
    by_code = {item["code"]: item for item in result["findings"]}

    assert by_code["short_connection_buffer"]["status"] == STATUS_RISK
    assert by_code["short_connection_buffer"]["evidence"]["wait_minutes"] == 120
    assert "не прогноз задержки" in by_code["short_connection_buffer"]["message"]


def test_variant_expansion_binds_price_baggage_and_checkout_to_exact_fare() -> None:
    raw = offer(
        "base-offer",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        baggage={"included": False, "pieces": 0},
    )
    raw["checkout_ref"] = {"offer_hash": "generic-not-a-variant", "passengers_full": 1}
    raw["variants"] = [
        {
            "variant_id": "light",
            "price": {"amount": 10_000, "currency": "RUB"},
            "conditions": {"baggage": {"included": False, "pieces": 0}},
            "checkout_ref": {"offer_hash": "light-ref", "passengers_full": 1},
        },
        {
            "variant_id": "flex",
            "price": {"amount": 14_000, "currency": "RUB"},
            "conditions": {"baggage": {"included": True, "pieces": 1}},
            "checkout_ref": {"offer_hash": "flex-ref", "passengers_full": 1},
        },
    ]

    fares = expand_offer_variants(raw)
    by_variant = {item.variant_id: item for item in fares}
    assert set(by_variant) == {"light", "flex"}
    assert by_variant["light"].price_amount == 10_000
    assert by_variant["flex"].price_amount == 14_000
    assert by_variant["flex"].baggage.checked_pieces == 1
    assert by_variant["flex"].checkout_ref == {"offer_hash": "flex-ref", "passengers_full": 1}
    assert by_variant["light"].checkout_ref != raw["checkout_ref"]


def test_bus_compact_variants_keep_offer_level_checkout_ref() -> None:
    raw = {
        "id": "bus-1",
        "transport": "bus",
        "checkout_ref": {
            "offer_hash": "bus-ref",
            "passengers_adult": 1,
            "city_from": "Казань",
            "city_to": "Москва",
        },
        "variants": [{"variant_id": "default", "price": {"amount": 2100, "currency": "RUB"}}],
        "legs": [
            {
                "from": "Автовокзал Восточный",
                "to": "Автовокзал Котельники",
                "departure_at": "2026-09-10T20:00:00+03:00",
                "arrival_at": "2026-09-11T11:00:00+03:00",
            }
        ],
    }
    fares = expand_offer_variants(raw, mode="bus")
    assert len(fares) == 1
    assert fares[0].checkout_ref == raw["checkout_ref"]
    assert fares[0].mode == "bus"


def _bus_offer(
    identifier: str,
    *,
    city_from: str,
    city_to: str,
    stop_from: str,
    stop_to: str,
    departure: str,
    arrival: str,
) -> dict:
    return {
        "offer_id": identifier,
        "transport": "bus",
        "checkout_ref": {
            "offer_hash": f"{identifier}-ref",
            "passengers_adult": 1,
            "city_from": city_from,
            "city_to": city_to,
        },
        "legs": [
            {
                "from": stop_from,
                "to": stop_to,
                "departure_at": departure,
                "arrival_at": arrival,
            }
        ],
        "price": {"amount": 900, "currency": "RUB"},
    }


def test_solver_matches_bus_stops_by_offer_level_city() -> None:
    """Bus segments name only the stop; the city lives in the checkout ref."""

    common = _bus_offer(
        "common",
        city_from="Москва",
        city_to="Владимир",
        stop_from="Автовокзал Центральный (Щёлковский)",
        stop_to="Автовокзал",
        departure="2026-09-10T15:00:00+03:00",
        arrival="2026-09-10T18:30:00+03:00",
    )
    feeder = _bus_offer(
        "feeder",
        city_from="Тула",
        city_to="Москва",
        stop_from="ост. Кирпичный завод",
        stop_to="Международный автовокзал Саларьево",
        departure="2026-09-10T09:00:00+03:00",
        arrival="2026-09-10T13:00:00+03:00",
    )
    contract = {
        "participants": [{"id": "anya", "origin": "Тула"}],
        "hub_code": "Москва",
        "destination_code": "Владимир",
        "min_wait_minutes": 60,
        "max_wait_minutes": 240,
    }

    solution = solve_group_rendezvous(contract, [common], {"anya": [feeder]}, max_scenarios=1)

    assert solution["scenarios"], solution["excluded_summary"]


def test_tutu_avia_variant_ref_overrides_only_documented_fare_fields() -> None:
    """Compact avia returns parent ref + per-fare offer_hash/service_class."""

    raw = {
        "offer_id": "parent-search-result",
        "transport": "avia",
        "checkout_ref": {
            "offer_hash": "parent-hash",
            "service_class": "ECONOMIC",
            "passengers_full": 2,
            "passengers_child": 0,
            "passengers_infant": 0,
            "is_round_trip": True,
            "return_departure_at": "2026-09-20T10:00:00+03:00",
        },
        "legs": [
            {
                "segments": [
                    {
                        "from": "Москва — Внуково (VKO), терм. A",
                        "to": "Стамбул — Новый аэропорт (IST), терм. B",
                        "departure_at": "2026-09-10T06:10:00+03:00",
                        "arrival_at": "2026-09-10T10:10:00+03:00",
                        "carrier": "TK",
                        "voyage_no": "TK-101",
                    }
                ]
            }
        ],
        "variants": [
            {
                "variant_id": "fare-flex",
                "offer_hash": "fare-hash",
                "service_class": "BUSINESS",
                "price": {"amount": 20_000, "currency": "RUB"},
                "conditions": {"baggage": {"pieces": 1}},
            }
        ],
    }

    expanded = expand_offer_variants(raw)
    assert len(expanded) == 1
    selected = expanded[0]
    assert selected.id == "fare-hash"
    assert selected.variant_id == "fare-flex"
    assert selected.mode == "avia"
    assert selected.segments[0].origin_code == "VKO"
    assert selected.segments[0].destination_code == "IST"
    assert selected.segments[0].service_number == "TK-101"
    assert selected.checkout_ref == {
        "offer_hash": "fare-hash",
        "service_class": "BUSINESS",
        "passengers_full": 2,
        "passengers_child": 0,
        "passengers_infant": 0,
        "is_round_trip": True,
        "return_departure_at": "2026-09-20T10:00:00+03:00",
    }


def test_checkout_guard_requires_precise_explicit_selection_and_preserves_counts() -> None:
    selected = offer("selected", origin="IST", destination="LHR", departure="2026-08-21T13:30:00+03:00", arrival="2026-08-21T16:05:00+01:00", passengers_full=3)
    ref = selected["checkout_ref"]

    blocked = checkout_handoff_guard(selected, explicit_selection=False, selected_checkout_ref=ref)
    assert blocked["allowed"] is False
    assert "explicit_fare_selection_required" in blocked["errors"]

    forged = checkout_handoff_guard(
        selected,
        explicit_selection=True,
        selected_checkout_ref={**ref, "offer_hash": "other"},
        expected_passengers={"passengers_full": 3, "passengers_child": 0, "passengers_infant": 0},
    )
    assert forged["allowed"] is False
    assert "selected_checkout_ref_does_not_match_offer" in forged["errors"]

    approved = checkout_handoff_guard(
        selected,
        explicit_selection=True,
        selected_checkout_ref=ref,
        expected_passengers={"passengers_full": 3, "passengers_child": 0, "passengers_infant": 0},
    )
    assert approved["allowed"] is True
    assert approved["checkout_ref"] == ref


def test_checkout_guard_allows_rail_ref_without_passenger_fields() -> None:
    selected = offer(
        "rail-direct",
        origin="2006004",
        destination="2004001",
        departure="2026-09-10T06:00:00+03:00",
        arrival="2026-09-10T12:13:00+03:00",
    )
    selected["mode"] = "rail"
    selected["checkout_ref"] = {
        "offer_hash": "rail-hash",
        "train_number": "742",
        "transport": "railway",
    }
    approved = checkout_handoff_guard(
        selected,
        explicit_selection=True,
        selected_checkout_ref=selected["checkout_ref"],
        expected_passengers={"passengers_full": 1},
    )
    assert approved["allowed"] is True


def test_service_checkout_resolves_server_side_variant_and_never_accepts_foreign_ref() -> None:
    selected = offer(
        "exact-flex",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=3,
    )
    feeder = offer(
        "moscow-to-ist",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T09:30:00+03:00",
    )
    original_ref = selected["checkout_ref"]
    fake = FakeTutuMcpClient(
        {
            "search_avia": lambda _name, args: {"offers": [selected] if args.get("origin") == "IST" else [feeder]},
            "create_checkout_link": lambda _name, args: {
                "checkout_url": "https://www.tutu.ru/avia/checkout/exact-flex",
                "kind": "checkout_deeplink",
                "observed_ref": args,
            }
        }
    )

    async def runner(_text: str) -> dict:
        observed = service.search_avia({"origin": "IST", "destination": "LHR", "adults": 3})
        feeder_result = service.search_avia({"origin": "VKO", "destination": "IST", "adults": 1})
        solution = service.solve_group_rendezvous(
            {
                "contract": {
                    "participants": [{"id": "moscow", "origin": "VKO"}],
                    "hub_code": "IST",
                    "destination_code": "LHR",
                    "min_wait_minutes": 120,
                    "max_wait_minutes": 300,
                },
                "common_offers": observed["offers"],
                "feeders_by_participant": {"moscow": feeder_result["offers"]},
            }
        )
        return {
            "summary": "Подобран вариант.",
            "presentation": solution,
            "checkout_components": [
                {
                    "component_ref": "shared-ist-lhr",
                    "expected_passengers": {"passengers_full": 3, "passengers_child": 0, "passengers_infant": 0},
                    "variants": [
                        {
                            "variant_id": "flex",
                            "checkout_ref": observed["offers"][0]["checkout_ref"],
                            "offer": observed["offers"][0],
                            "name": "Flex",
                        }
                    ],
                }
            ],
        }

    service = GroupSyncService(tutu=TutuMcpGateway(fake), agent_runner=runner)

    async def exercise() -> tuple[dict, dict]:
        rendered = await service.run("Найдите нам общий рейс")
        handoff = await service.create_checkout_link(rendered["run_id"], "shared-ist-lhr", "flex")
        return rendered, handoff

    rendered, handoff = asyncio.run(exercise())
    assert "checkout_ref" not in rendered
    assert handoff["url"] == "https://www.tutu.ru/avia/checkout/exact-flex"
    assert handoff["kind"] == "deeplink"
    assert fake.calls == [
        ("search_avia", {"origin": "IST", "destination": "LHR", "adults": 3}),
        ("search_avia", {"origin": "VKO", "destination": "IST", "adults": 1}),
        ("create_checkout_link", original_ref),
    ]

    with pytest.raises(CheckoutSelectionError):
        asyncio.run(service.create_checkout_link(rendered["run_id"], "shared-ist-lhr", "foreign-variant"))
    assert len(fake.calls) == 3


def test_service_turns_solver_scenario_into_safe_clickable_booking_unit() -> None:
    selected = offer(
        "auto-common",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=2,
    )
    feeder = offer(
        "auto-feeder",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T09:30:00+03:00",
    )
    fake = FakeTutuMcpClient(
        {
            "search_avia": lambda _name, args: {"offers": [selected] if args.get("origin") == "IST" else [feeder]},
            "create_checkout_link": {
                "checkout_url": "https://www.tutu.ru/avia/checkout/auto-common",
                "kind": "search_redirect",
            }
        }
    )

    async def runner(_text: str) -> dict:
        observed = service.search_avia({"origin": "IST", "destination": "LHR", "adults": 2})
        feeder_result = service.search_avia({"origin": "VKO", "destination": "IST", "adults": 1})
        solution = service.solve_group_rendezvous(
            {
                "contract": {
                    "participants": [{"id": "moscow", "origin": "VKO"}],
                    "hub_code": "IST",
                    "destination_code": "LHR",
                    "min_wait_minutes": 120,
                    "max_wait_minutes": 300,
                },
                "common_offers": observed["offers"],
                "feeders_by_participant": {"moscow": feeder_result["offers"]},
            }
        )
        solution["scenarios"][0]["id"] = "balance"
        return solution

    service = GroupSyncService(tutu=TutuMcpGateway(fake), agent_runner=runner)

    async def exercise() -> tuple[dict, dict]:
        rendered = await service.run("Общий рейс")
        unit = rendered["scenarios"][0]["booking_units"][0]
        tariff = unit["tariffs"][0]
        handoff = await service.create_checkout_link(rendered["run_id"], unit["component_ref"], tariff["variant_id"])
        return rendered, handoff

    rendered, handoff = asyncio.run(exercise())
    tariff = rendered["scenarios"][0]["booking_units"][0]["tariffs"][0]
    assert tariff["variant_id"] == "auto-common"
    assert "checkout_ref" not in tariff
    assert handoff["kind"] == "search_redirect"


def test_service_drops_checkout_ref_that_model_did_not_observe_from_tutu() -> None:
    observed = offer(
        "observed",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
    )
    forged = {**observed["checkout_ref"], "offer_hash": "model-invented"}
    fake = FakeTutuMcpClient({"search_avia": {"offers": [observed]}})

    async def runner(_text: str) -> dict:
        service.search_avia({"origin": "IST", "destination": "LHR"})
        return {
            "checkout_components": [
                {
                    "component_ref": "foreign",
                    "variants": [
                        {"variant_id": "forged", "checkout_ref": forged, "offer": observed}
                    ],
                }
            ]
        }

    service = GroupSyncService(tutu=TutuMcpGateway(fake), agent_runner=runner)

    rendered = asyncio.run(service.run("Не доверяй модели checkout_ref"))
    assert "checkout_ref" not in rendered
    with pytest.raises(CheckoutSelectionError):
        asyncio.run(service.create_checkout_link(rendered["run_id"], "foreign", "forged"))
    assert fake.calls == [("search_avia", {"origin": "IST", "destination": "LHR"})]


def test_gateway_is_fake_friendly_and_forwards_search_arguments() -> None:
    fake = FakeTutuMcpClient({"search_avia": {"offers": []}})
    gateway = TutuMcpGateway(fake)

    assert gateway.search_avia(origin="VKO", destination="IST", adults=2) == {"offers": []}
    assert fake.calls == [("search_avia", {"origin": "VKO", "destination": "IST", "adults": 2})]


def test_gateway_expands_the_stored_checkout_ref_for_tutu_live_schema() -> None:
    checkout_ref = {
        "transport": "avia",
        "offer_hash": "opaque-offer",
        "passengers_full": 2,
        "service_class": "ECONOMIC",
    }
    fake = FakeTutuMcpClient({"create_checkout_link": {"checkout_url": "https://www.tutu.ru/checkout"}})
    gateway = TutuMcpGateway(fake)

    assert gateway.create_checkout_link(checkout_ref)["checkout_url"] == "https://www.tutu.ru/checkout"
    assert fake.calls == [("create_checkout_link", checkout_ref)]
    with pytest.raises(ValueError):
        gateway.create_checkout_link()
    with pytest.raises(ValueError):
        gateway.create_checkout_link(checkout_ref, offer_hash="override")


def test_streamable_http_client_handles_sse_session_and_structured_content() -> None:
    class Response:
        def __init__(self, text: str, *, headers: dict[str, str] | None = None, status_code: int = 200) -> None:
            self.text = text
            self.headers = headers or {}
            self.status_code = status_code

    class Session:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def post(self, _url: str, *, json: dict, headers: dict, timeout: float) -> Response:
            self.calls.append({"json": json, "headers": headers, "timeout": timeout})
            method = json["method"]
            if method == "initialize":
                body = {"jsonrpc": "2.0", "id": json["id"], "result": {"serverInfo": {"name": "tutu"}}}
                return Response(f"event: message\ndata: {json_module.dumps(body)}\n\n", headers={"Mcp-Session-Id": "session-1"})
            if method == "notifications/initialized":
                return Response("", status_code=202)
            progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 50}}
            body = {
                "jsonrpc": "2.0",
                "id": json["id"],
                "result": {"structuredContent": {"offers": []}},
            }
            return Response(
                "event: message\n"
                f"data: {json_module.dumps(progress)}\n\n"
                "event: message\n"
                f"data: {json_module.dumps([body])}\n\n"
            )

    session = Session()
    client = StreamableHttpMcpClient(endpoint="https://example.test/mcp", session=session)

    assert client.call_tool("search_avia", {"origin": "VKO", "destination": "IST"}) == {"offers": []}
    assert [call["json"]["method"] for call in session.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert session.calls[-1]["headers"]["Mcp-Session-Id"] == "session-1"


def test_streamable_http_client_reinitializes_once_after_expired_session_404() -> None:
    class Response:
        def __init__(self, text: str, *, headers: dict[str, str] | None = None, status_code: int = 200) -> None:
            self.text = text
            self.headers = headers or {}
            self.status_code = status_code

    class ExpiringSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.initialize_count = 0

        def post(self, _url: str, *, json: dict, headers: dict, timeout: float) -> Response:
            self.calls.append({"json": json, "headers": headers, "timeout": timeout})
            method = json["method"]
            if method == "initialize":
                self.initialize_count += 1
                session_id = f"session-{self.initialize_count}"
                body = {"jsonrpc": "2.0", "id": json["id"], "result": {"serverInfo": {"name": "tutu"}}}
                return Response(json_module.dumps(body), headers={"Mcp-Session-Id": session_id})
            if method == "notifications/initialized":
                return Response("", status_code=202)
            if headers.get("Mcp-Session-Id") == "session-1":
                return Response("expired", status_code=404)
            body = {"jsonrpc": "2.0", "id": json["id"], "result": {"structuredContent": {"offers": ["fresh"]}}}
            return Response(json_module.dumps(body))

    session = ExpiringSession()
    client = StreamableHttpMcpClient(endpoint="https://example.test/mcp", session=session)

    assert client.call_tool("search_avia", {"origin": "VKO", "destination": "IST"}) == {"offers": ["fresh"]}
    assert [call["json"]["method"] for call in session.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert session.calls[-1]["headers"]["Mcp-Session-Id"] == "session-2"

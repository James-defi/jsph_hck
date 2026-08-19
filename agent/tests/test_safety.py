"""SafetyGate / RecommendationPolicy regressions for CTA and checkout."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.app.application import build_live_service
from agent.app.config import Settings
from agent.app.models import AssistantTurn, ProviderMessage, ToolCall
from agent.app.safety import RecommendationPolicy
from agent.app.service import CheckoutSelectionError
from agent.app.tutu_mcp import FakeTutuMcpClient, TutuMcpGateway
from agent.app.web import create_app


class ScriptedLLM:
    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns
        self.calls: list[tuple[list[ProviderMessage], list[dict[str, Any]]]] = []

    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> AssistantTurn:
        self.calls.append((list(messages), tools))
        return self.turns.pop(0)


def _offer(
    identifier: str,
    *,
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    passengers_full: int,
    price: int,
    mode: str = "avia",
) -> dict[str, Any]:
    ref = {
        "offer_hash": identifier,
        "passengers_full": passengers_full,
        "passengers_child": 0,
        "passengers_infant": 0,
        "provider_specific": {"preserve": identifier},
    }
    return {
        "id": identifier,
        "transport": mode,
        "mode": mode,
        "price": {"amount": price, "currency": "RUB"},
        "legs": [
            {
                "segments": [
                    {
                        "origin_code": origin,
                        "destination_code": destination,
                        "departure_at": departure,
                        "arrival_at": arrival,
                        "carrier": "TK",
                        "flight_number": identifier,
                    }
                ]
            }
        ],
        "is_multi_pnr": False,
        "conditions": {"baggage": {"included": True, "pieces": 1}},
        "variants": [
            {
                "variant_id": f"{identifier}-flex",
                "offer_hash": f"{identifier}-fare-hash",
                "service_class": "ECONOMIC",
                "name": "Flex",
                "price": {"amount": price, "currency": "RUB"},
                "conditions": {"baggage": {"included": True, "pieces": 1}, "changeable": True},
            }
        ],
        "checkout_ref": ref,
    }


def _contract(*, min_wait: int = 120, max_wait: int = 300) -> dict[str, Any]:
    return {
        "participants": [
            {"id": "Аня", "origin": "VKO"},
            {"id": "Илья", "origin": "LED"},
        ],
        "hub_code": "IST",
        "destination_code": "LHR",
        "min_wait_minutes": min_wait,
        "max_wait_minutes": max_wait,
        "required_checked_baggage_pieces": 1,
        "strict_baggage": True,
    }


def _plan_turn(contract: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[
            ToolCall(
                id="plan-1",
                name="plan_group_sync",
                arguments={
                    "departure_date": "2026-08-21",
                    "common_mode": "avia",
                    "contract": contract,
                },
            )
        ]
    )


def _live_service(
    *,
    moscow_arrival: str,
    petersburg_arrival: str = "2026-08-21T09:20:00+03:00",
    common_departure: str = "2026-08-21T13:30:00+03:00",
    contract: dict[str, Any] | None = None,
) -> tuple[Any, ScriptedLLM, FakeTutuMcpClient]:
    common = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure=common_departure,
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=2,
        price=30000,
    )
    moscow = _offer(
        "TK101",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:10:00+03:00",
        arrival=moscow_arrival,
        passengers_full=1,
        price=11000,
    )
    petersburg = _offer(
        "TK102",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival=petersburg_arrival,
        passengers_full=1,
        price=12000,
    )

    def search_avia(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        pair = (arguments["origin"], arguments["destination"])
        offers = {("IST", "LHR"): [common], ("VKO", "IST"): [moscow], ("LED", "IST"): [petersburg]}[pair]
        return {"offers": offers}

    fake = FakeTutuMcpClient(
        {
            "get_avia_instructions": {"text": "search instructions"},
            "search_avia": search_avia,
            "create_checkout_link": lambda _name, arguments: {
                "checkout_url": "https://www.tutu.ru/avia/checkout/TK1983",
                "kind": "checkout_deeplink",
                "observed_ref": arguments,
            },
        }
    )
    llm = ScriptedLLM([_plan_turn(contract or _contract())])
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    return service, llm, fake


def _solver_shaped_scenario(
    *,
    wait_minutes: int | None,
    mode: str = "avia",
    arrival_point: str = "IST",
    common_origin: str = "IST",
    arrival_at: str | None = "2026-08-21T11:31:00+03:00",
    departure_at: str | None = "2026-08-21T13:30:00+03:00",
    checkout: bool = True,
    is_multi_pnr: bool = False,
) -> dict[str, Any]:
    def offer_body(identifier: str, origin: str, destination: str, depart: str | None, arrive: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": identifier,
            "mode": mode,
            "transport": mode,
            "is_multi_pnr": is_multi_pnr,
            "baggage": {"included": True, "checked_pieces": 1},
            "segments": [
                {
                    "origin_code": origin,
                    "destination_code": destination,
                    "departure_at": depart,
                    "arrival_at": arrive,
                    "mode": mode,
                }
            ],
        }
        if checkout:
            body["checkout_ref"] = {"offer_hash": identifier, "passengers_full": 1}
        return body

    return {
        "common_offer": offer_body("common", common_origin, "LHR", departure_at, "2026-08-21T16:05:00+01:00"),
        "common_service_signature": {
            "mode": mode,
            "origin_code": common_origin,
            "destination_code": "LHR",
            "departure_at": departure_at,
        },
        "feeders": [
            {
                "participant_id": "moscow",
                "wait_minutes": wait_minutes,
                "offer": offer_body("feeder", "VKO", arrival_point, "2026-08-21T06:10:00+03:00", arrival_at),
            }
        ],
    }


def _solo_contract(*, hub: str = "IST") -> dict[str, Any]:
    return {
        "participants": [{"id": "moscow", "origin": "VKO"}],
        "hub_code": hub,
        "destination_code": "LHR",
        "min_wait_minutes": 0,
        "max_wait_minutes": 24 * 60,
    }


def test_air_self_transfer_below_floor_is_blocked_with_actual_and_required_minutes() -> None:
    policy = RecommendationPolicy()
    for wait in (119, 200):
        verdict = policy.evaluate(_solo_contract(), _solver_shaped_scenario(wait_minutes=wait))
        assert verdict.status == "blocked"
        assert str(wait) in verdict.reason
        assert "240" in verdict.reason
        assert "не покажем" in verdict.reason.lower() or "не покажем" in verdict.reason


def test_air_self_transfer_of_240_minutes_can_be_recommended() -> None:
    verdict = RecommendationPolicy().evaluate(
        _solo_contract(),
        _solver_shaped_scenario(wait_minutes=240, arrival_at="2026-08-21T09:30:00+03:00"),
    )
    assert verdict.status == "recommended"


def test_rail_and_bus_floors_and_unknown_transfer_point() -> None:
    policy = RecommendationPolicy()
    rail_short = policy.evaluate(
        _solo_contract(),
        _solver_shaped_scenario(wait_minutes=20, mode="rail"),
    )
    assert rail_short.status == "blocked"
    assert "20" in rail_short.reason
    assert "30" in rail_short.reason

    rail_ok = policy.evaluate(
        _solo_contract(),
        _solver_shaped_scenario(wait_minutes=30, mode="rail", arrival_at="2026-08-21T13:00:00+03:00"),
    )
    assert rail_ok.status == "recommended"

    bus_short = policy.evaluate(
        _solo_contract(),
        _solver_shaped_scenario(wait_minutes=40, mode="bus"),
    )
    assert bus_short.status == "blocked"
    assert "45" in bus_short.reason

    different_station = policy.evaluate(
        _solo_contract(),
        _solver_shaped_scenario(wait_minutes=120, mode="rail", arrival_point="SAW", common_origin="IST"),
    )
    assert different_station.status in {"blocked", "needs_verification"}

    missing_times = policy.evaluate(
        _solo_contract(),
        _solver_shaped_scenario(wait_minutes=None, arrival_at=None, departure_at=None),
    )
    assert missing_times.status == "needs_verification"


def test_live_200_minute_air_connection_has_no_cta_and_checkout_is_422() -> None:
    service, llm, fake = _live_service(moscow_arrival="2026-08-21T10:10:00+03:00")
    result = asyncio.run(service.run("Нас двое через IST в LHR 21 августа"))

    assert result["scenarios"]
    for scenario in result["scenarios"]:
        assert scenario["safety_verdict"] == "blocked"
        assert scenario.get("booking_units") in ([], None)
        reason = scenario.get("safety_reason") or scenario.get("subtitle") or ""
        assert "200" in reason
        assert "240" in reason
        tariffs = [
            tariff
            for unit in scenario.get("booking_units") or []
            for tariff in unit.get("tariffs") or []
        ]
        assert tariffs == []
    public = json.dumps(result, ensure_ascii=False)
    assert "checkout_ref" not in public
    assert "provider_specific" not in public
    stored = service.run_store.get(result["run_id"])
    assert stored.components == {}

    with pytest.raises(CheckoutSelectionError):
        asyncio.run(service.create_checkout_link(result["run_id"], "scenario-1:common", "TK1983-flex"))
    assert all(name != "create_checkout_link" for name, _arguments in fake.calls)
    assert llm.calls  # planner ran; this is a safety block, not an injection refusal

    client = TestClient(create_app(service))
    api = client.post(
        "/api/checkout",
        json={
            "run_id": result["run_id"],
            "component_ref": "scenario-1:common",
            "variant_id": "TK1983-flex",
        },
    )
    assert api.status_code == 422
    assert all(name != "create_checkout_link" for name, _arguments in fake.calls)


def test_live_240_minute_fixture_remains_recommended_and_can_checkout() -> None:
    service, _llm, fake = _live_service(moscow_arrival="2026-08-21T09:30:00+03:00")
    result = asyncio.run(service.run("Нас двое через IST в LHR 21 августа"))

    assert result["scenarios"]
    assert result["scenarios"][0]["safety_verdict"] == "recommended"
    assert result["scenarios"][0]["booking_units"]
    public = json.dumps(result, ensure_ascii=False)
    assert "checkout_ref" not in public

    handoff = asyncio.run(service.create_checkout_link(result["run_id"], "scenario-1:common", "TK1983-flex"))
    assert handoff["url"] == "https://www.tutu.ru/avia/checkout/TK1983"
    assert fake.calls[-1][0] == "create_checkout_link"


def _ticket_offer(
    *,
    identifier: str,
    segments: list[dict[str, Any]],
    mode: str = "avia",
    is_multi_pnr: bool = False,
    checkout: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": identifier,
        "mode": mode,
        "transport": mode,
        "is_multi_pnr": is_multi_pnr,
        "segments": segments,
        "price": {"amount": 12_000, "currency": "RUB"},
    }
    if checkout:
        body["checkout_ref"] = {"offer_hash": identifier, "passengers_full": 1}
    return body


def test_evaluate_offer_direct_air_with_tariff_ref_is_recommended() -> None:
    verdict = RecommendationPolicy().evaluate_offer(
        _ticket_offer(
            identifier="SU1",
            segments=[
                {
                    "origin_code": "VKO",
                    "destination_code": "LED",
                    "departure_at": "2026-09-25T08:00:00+03:00",
                    "arrival_at": "2026-09-25T09:30:00+03:00",
                    "mode": "avia",
                }
            ],
        ),
        max_wait_minutes=24 * 60,
    )
    assert verdict.status == "recommended"


def test_evaluate_offer_three_segment_air_90_minute_connection_is_blocked() -> None:
    verdict = RecommendationPolicy().evaluate_offer(
        _ticket_offer(
            identifier="AER-LIS",
            segments=[
                {
                    "origin_code": "AER",
                    "destination_code": "IST",
                    "departure_at": "2026-09-25T06:00:00+03:00",
                    "arrival_at": "2026-09-25T08:00:00+03:00",
                    "mode": "avia",
                },
                {
                    "origin_code": "IST",
                    "destination_code": "MAD",
                    "departure_at": "2026-09-25T09:30:00+03:00",
                    "arrival_at": "2026-09-25T13:00:00+01:00",
                    "mode": "avia",
                },
                {
                    "origin_code": "MAD",
                    "destination_code": "LIS",
                    "departure_at": "2026-09-25T17:00:00+01:00",
                    "arrival_at": "2026-09-25T18:20:00+01:00",
                    "mode": "avia",
                },
            ],
        ),
        max_wait_minutes=24 * 60,
    )
    assert verdict.status == "blocked"
    assert "90" in verdict.reason
    assert "240" in verdict.reason
    assert "нужен запас не меньше" in verdict.reason


def test_evaluate_offer_rail_and_bus_direct_or_floor_can_be_recommended() -> None:
    policy = RecommendationPolicy()
    rail = policy.evaluate_offer(
        _ticket_offer(
            identifier="RZD1",
            mode="rail",
            segments=[
                {
                    "origin_code": "MOW",
                    "destination_code": "LED",
                    "departure_at": "2026-09-25T08:00:00+03:00",
                    "arrival_at": "2026-09-25T12:00:00+03:00",
                    "mode": "rail",
                }
            ],
        ),
        max_wait_minutes=24 * 60,
    )
    assert rail.status == "recommended"
    bus_wait = policy.evaluate_offer(
        _ticket_offer(
            identifier="BUS1",
            mode="bus",
            segments=[
                {
                    "origin_code": "KZN",
                    "destination_code": "MOW",
                    "departure_at": "2026-09-25T08:00:00+03:00",
                    "arrival_at": "2026-09-25T12:00:00+03:00",
                    "mode": "bus",
                },
                {
                    "origin_code": "MOW",
                    "destination_code": "LED",
                    "departure_at": "2026-09-25T12:45:00+03:00",
                    "arrival_at": "2026-09-25T20:00:00+03:00",
                    "mode": "bus",
                },
            ],
        ),
        max_wait_minutes=24 * 60,
    )
    assert bus_wait.status == "recommended"

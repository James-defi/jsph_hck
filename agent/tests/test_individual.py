"""Solo A→B path: plan_individual_trip, SafetyGate on one offer, UI contract."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from fastapi.testclient import TestClient

from agent.app.application import LIVE_SYSTEM_PROMPT, build_live_service
from agent.app.config import Settings
from agent.app.models import AssistantTurn, ProviderMessage, ToolCall
from agent.app.tutu_mcp import FakeTutuMcpClient, TutuMcpGateway, TutuMcpToolError
from agent.app.web import create_app


DATE = "2026-09-25"


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
    passengers_full: int = 1,
    price: int = 8_000,
    mode: str = "avia",
    segments: list[dict[str, Any]] | None = None,
    is_multi_pnr: bool = False,
) -> dict[str, Any]:
    ref = {
        "offer_hash": identifier,
        "passengers_full": passengers_full,
        "passengers_child": 0,
        "passengers_infant": 0,
        "provider_specific": {"preserve": identifier},
    }
    carrier = {"avia": "SU", "rail": "RZD", "bus": "BUS"}.get(mode, "XX")
    built_segments = segments or [
        {
            "origin_code": origin,
            "destination_code": destination,
            "departure_at": departure,
            "arrival_at": arrival,
            "carrier": carrier,
            "mode": mode,
            "transport": mode,
        }
    ]
    variant: dict[str, Any] = {
        "variant_id": f"{identifier}-flex",
        "offer_hash": f"{identifier}-fare-hash",
        "service_class": "ECONOMIC",
        "name": "Flex",
        "price": {"amount": price, "currency": "RUB"},
        "conditions": {"baggage": {"included": True, "pieces": 1}, "changeable": True},
    }
    if mode != "avia":
        variant["checkout_ref"] = dict(ref)
    return {
        "id": identifier,
        "transport": mode,
        "mode": mode,
        "price": {"amount": price, "currency": "RUB"},
        "legs": [{"segments": built_segments}],
        "is_multi_pnr": is_multi_pnr,
        "conditions": {"baggage": {"included": True, "pieces": 1}},
        "variants": [variant],
        "checkout_ref": ref,
    }


def _plan_turn(*, mode: str, origin: str, destination: str) -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[
            ToolCall(
                id="plan-solo-1",
                name="plan_individual_trip",
                arguments={
                    "departure_date": DATE,
                    "mode": mode,
                    "origin": origin,
                    "destination": destination,
                    "adults": 1,
                },
            )
        ]
    )


def _build_stack(
    *,
    mode: str,
    origin: str,
    destination: str,
    offers: list[dict[str, Any]],
    turns: list[AssistantTurn] | None = None,
    checkout: bool = True,
) -> tuple[Any, ScriptedLLM, FakeTutuMcpClient]:
    def search(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert arguments["origin"] == origin
        assert arguments["destination"] == destination
        return {"offers": offers}

    responses: dict[str, Any] = {
        f"get_{mode}_instructions": {"text": f"{mode} search instructions"},
        f"search_{mode}": search,
    }
    if checkout:
        responses["create_checkout_link"] = lambda _name, arguments: {
            "checkout_url": f"https://www.tutu.ru/{mode}/checkout/{arguments.get('offer_hash') or 'selected'}",
            "kind": "checkout_deeplink",
            "observed_ref": arguments,
        }
    fake = FakeTutuMcpClient(responses)
    llm = ScriptedLLM(turns or [_plan_turn(mode=mode, origin=origin, destination=destination)])
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    return service, llm, fake


def _public_has_no_secrets(payload: dict[str, Any] | str) -> None:
    text = payload if isinstance(payload, str) else str(payload)
    assert "checkout_ref" not in text
    assert "provider_specific" not in text


def _tool_schema_names(llm: ScriptedLLM) -> list[str]:
    return [schema["function"]["name"] for schema in llm.calls[0][1]]


def test_air_solo_vko_led_direct_is_recommended_and_checkouts() -> None:
    offer = _offer(
        "SU100",
        origin="VKO",
        destination="LED",
        departure="2026-09-25T08:10:00+03:00",
        arrival="2026-09-25T09:40:00+03:00",
        mode="avia",
        price=7_400,
    )
    service, llm, fake = _build_stack(mode="avia", origin="VKO", destination="LED", offers=[offer])
    client = TestClient(create_app(service))
    response = client.post(
        "/api/search",
        json={"query": "Один взрослый 25 сентября 2026 самолётом из VKO в LED"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _public_has_no_secrets(response.text)
    assert body["scenarios"]
    scenario = body["scenarios"][0]
    assert scenario["safety_verdict"] == "recommended"
    assert scenario["booking_units"]
    unit = scenario["booking_units"][0]
    assert unit["component_ref"] == "scenario-1:ticket"
    assert unit["tariffs"]
    variant_id = unit["tariffs"][0]["variant_id"]
    assert "Поиск пока не был запущен." not in (body.get("rejection_summary") or "")

    names = _tool_schema_names(llm)
    assert "plan_individual_trip" in names
    assert "plan_group_sync" in names
    assert "search_avia" not in names
    assert [call[0] for call in fake.calls[:2]] == ["get_avia_instructions", "search_avia"]

    handoff = client.post(
        "/api/checkout",
        json={"run_id": body["run_id"], "component_ref": "scenario-1:ticket", "variant_id": variant_id},
    )
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["url"].startswith("https://www.tutu.ru/")
    assert fake.calls[-1][0] == "create_checkout_link"


def test_aer_lis_three_segment_90_minute_connection_is_blocked_without_cta() -> None:
    offer = _offer(
        "AERLIS90",
        origin="AER",
        destination="LIS",
        departure="2026-09-25T06:00:00+03:00",
        arrival="2026-09-25T18:20:00+01:00",
        mode="avia",
        price=41_000,
        segments=[
            {
                "origin_code": "AER",
                "destination_code": "IST",
                "departure_at": "2026-09-25T06:00:00+03:00",
                "arrival_at": "2026-09-25T08:00:00+03:00",
                "mode": "avia",
                "transport": "avia",
            },
            {
                "origin_code": "IST",
                "destination_code": "MAD",
                "departure_at": "2026-09-25T09:30:00+03:00",
                "arrival_at": "2026-09-25T13:00:00+01:00",
                "mode": "avia",
                "transport": "avia",
            },
            {
                "origin_code": "MAD",
                "destination_code": "LIS",
                "departure_at": "2026-09-25T17:00:00+01:00",
                "arrival_at": "2026-09-25T18:20:00+01:00",
                "mode": "avia",
                "transport": "avia",
            },
        ],
    )
    service, _llm, fake = _build_stack(mode="avia", origin="AER", destination="LIS", offers=[offer])
    client = TestClient(create_app(service))
    response = client.post(
        "/api/search",
        json={"query": "Один взрослый из AER в LIS со стыковкой около 90 минут"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _public_has_no_secrets(response.text)
    assert body["scenarios"]
    scenario = body["scenarios"][0]
    assert scenario["safety_verdict"] == "blocked"
    assert scenario.get("booking_units") in ([], None)
    reason = str(scenario.get("safety_reason") or scenario.get("subtitle") or "")
    assert "90" in reason
    assert "240" in reason
    assert "нужен запас не меньше" in reason
    checkout = client.post(
        "/api/checkout",
        json={"run_id": body["run_id"], "component_ref": "scenario-1:ticket", "variant_id": "AERLIS90-flex"},
    )
    assert checkout.status_code == 422
    assert all(name != "create_checkout_link" for name, _arguments in fake.calls)


def test_rail_and_bus_solo_direct_are_recommended_with_tariff_ref() -> None:
    rail = _offer(
        "RZD700",
        origin="MOW",
        destination="LED",
        departure="2026-09-25T07:00:00+03:00",
        arrival="2026-09-25T11:10:00+03:00",
        mode="rail",
        price=3_200,
    )
    bus = _offer(
        "BUS11",
        origin="KZN",
        destination="MOW",
        departure="2026-09-25T09:00:00+03:00",
        arrival="2026-09-25T19:30:00+03:00",
        mode="bus",
        price=2_100,
    )
    for mode, origin, destination, offer in (
        ("rail", "MOW", "LED", rail),
        ("bus", "KZN", "MOW", bus),
    ):
        service, _llm, fake = _build_stack(mode=mode, origin=origin, destination=destination, offers=[offer])
        client = TestClient(create_app(service))
        response = client.post(
            "/api/search",
            json={"query": f"Один взрослый {mode} {origin} → {destination} 25 сентября"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        _public_has_no_secrets(response.text)
        scenario = body["scenarios"][0]
        assert scenario["safety_verdict"] == "recommended", scenario
        unit = scenario["booking_units"][0]
        variant_id = unit["tariffs"][0]["variant_id"]
        handoff = client.post(
            "/api/checkout",
            json={
                "run_id": body["run_id"],
                "component_ref": unit["component_ref"],
                "variant_id": variant_id,
            },
        )
        assert handoff.status_code == 200, handoff.text
        assert fake.calls[-1][0] == "create_checkout_link"


def test_llm_cannot_call_search_avia_because_it_is_not_in_tools() -> None:
    offer = _offer(
        "SU100",
        origin="VKO",
        destination="LED",
        departure="2026-09-25T08:10:00+03:00",
        arrival="2026-09-25T09:40:00+03:00",
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="illegal-search",
                        name="search_avia",
                        arguments={"origin": "VKO", "destination": "LED", "departure_date": DATE},
                    )
                ]
            ),
            AssistantTurn(content="Нашёл рейс без карточек.", finish_reason="stop"),
        ]
    )
    fake = FakeTutuMcpClient(
        {
            "get_avia_instructions": {"text": "search instructions"},
            "search_avia": {"offers": [offer]},
        }
    )
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    client = TestClient(create_app(service))
    response = client.post("/api/search", json={"query": "Один из VKO в LED"})
    assert response.status_code == 200
    body = response.json()
    names = _tool_schema_names(llm)
    assert "search_avia" not in names
    assert "get_offer_details" not in names
    assert "inspect_offer_risks" not in names
    assert "solve_group_rendezvous" not in names
    assert "plan_individual_trip" in names
    assert fake.calls == []
    assert body["scenarios"] == []
    assert "Поиск пока не был запущен." in (body.get("rejection_summary") or "")


def test_fallback_summary_has_no_tutu_url_when_llm_writes_markdown() -> None:
    llm = ScriptedLLM(
        [
            AssistantTurn(
                content=(
                    "Подобрал рейс VKO → LED: https://avia.tutu.ru/offer?from=VKO&to=LED "
                    "и ещё https://www.tutu.ru/rail/"
                ),
                finish_reason="stop",
            )
        ]
    )
    fake = FakeTutuMcpClient({"search_avia": {"offers": []}})
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    client = TestClient(create_app(service))
    response = client.post("/api/search", json={"query": "Один взрослый из VKO в LED"})
    assert response.status_code == 200
    body = response.json()
    public = json.dumps(body, ensure_ascii=False).lower()
    assert "tutu.ru" not in public
    assert "http://" not in public
    assert "https://" not in body["summary"].lower()
    assert body["scenarios"] == []
    assert body["summary"]
    assert "Поиск пока не был запущен." in (body.get("rejection_summary") or "")


RAIL_CITY_INSTRUCTIONS = {
    "text": (
        "Для search_rail указывай станции с кодами, например:\n"
        'origin="Москва — Ленинградский вокзал (2006004)"\n'
        'destination="Санкт-Петербург — Московский вокзал (2004004)"\n'
    )
}


def test_prompt_asks_to_call_plan_individual_with_city_names() -> None:
    assert "названия городов" in LIVE_SYSTEM_PROMPT
    assert "спрашивай вокзал до вызова tool" in LIVE_SYSTEM_PROMPT
    assert "search_rail" in LIVE_SYSTEM_PROMPT


def test_rail_city_names_resolve_from_instructions_and_search() -> None:
    offer = _offer(
        "RZD742",
        origin="2006004",
        destination="2004004",
        departure="2026-09-25T06:00:00+03:00",
        arrival="2026-09-25T12:13:00+03:00",
        mode="rail",
        price=1_623,
    )
    observed: list[dict[str, Any]] = []

    def search(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        observed.append(dict(arguments))
        assert "2006004" in str(arguments["origin"])
        assert "2004004" in str(arguments["destination"])
        return {"offers": [offer]}

    fake = FakeTutuMcpClient(
        {
            "get_rail_instructions": RAIL_CITY_INSTRUCTIONS,
            "search_rail": search,
            "create_checkout_link": lambda _name, arguments: {
                "checkout_url": f"https://www.tutu.ru/rail/checkout/{arguments.get('offer_hash') or 'selected'}",
                "kind": "checkout_deeplink",
                "observed_ref": arguments,
            },
        }
    )
    llm = ScriptedLLM([_plan_turn(mode="rail", origin="Москва", destination="Санкт-Петербург")])
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    client = TestClient(create_app(service))
    response = client.post(
        "/api/search",
        json={"query": "Один взрослый, 25 сентября 2026 года, поездом из Москвы в Санкт-Петербург"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _public_has_no_secrets(response.text)
    assert body["scenarios"]
    assert body["scenarios"][0]["safety_verdict"] == "recommended"
    assert observed and "2006004" in observed[0]["origin"]
    assert [call[0] for call in fake.calls[:2]] == ["get_rail_instructions", "search_rail"]
    names = _tool_schema_names(llm)
    assert "plan_individual_trip" in names
    assert "search_rail" not in names
    assert "get_rail_instructions" not in names

    unit = body["scenarios"][0]["booking_units"][0]
    variant_id = unit["tariffs"][0]["variant_id"]
    handoff = client.post(
        "/api/checkout",
        json={"run_id": body["run_id"], "component_ref": unit["component_ref"], "variant_id": variant_id},
    )
    assert handoff.status_code == 200, handoff.text
    assert fake.calls[-1][0] == "create_checkout_link"


def test_ambiguous_rail_stations_do_not_search_or_offer_cta() -> None:
    fake = FakeTutuMcpClient(
        {
            "get_rail_instructions": {
                "text": (
                    "Москва — Казанский вокзал (2000001)\n"
                    "Москва — Ярославский вокзал (2000003)\n"
                    "Казань — Вокзал (2060001)\n"
                )
            },
            "search_rail": {"offers": [{"id": "should-not-run"}]},
        }
    )
    llm = ScriptedLLM([_plan_turn(mode="rail", origin="Москва", destination="Казань")])
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    client = TestClient(create_app(service))
    response = client.post(
        "/api/search",
        json={"query": "Один взрослый поездом из Москвы в Казань 25 сентября 2026"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _public_has_no_secrets(response.text)
    assert body["scenarios"] == []
    assert body.get("booking_units") in ([], None)
    assert "Казанский" in body["summary"]
    assert "Ярославский" in body["summary"]
    assert "Поиск не запускался" in (body.get("rejection_summary") or "")
    assert [name for name, _arguments in fake.calls] == ["get_rail_instructions"]
    checkout = client.post(
        "/api/checkout",
        json={"run_id": body["run_id"], "component_ref": "scenario-1:ticket", "variant_id": "missing"},
    )
    assert checkout.status_code == 422


def test_tutu_rejects_city_passthrough_then_clarifies_without_cta() -> None:
    fake = FakeTutuMcpClient(
        {
            "get_rail_instructions": {"text": "rail search instructions"},
            "search_rail": TutuMcpToolError("unknown station Москва"),
        }
    )
    llm = ScriptedLLM([_plan_turn(mode="rail", origin="Москва", destination="Санкт-Петербург")])
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    client = TestClient(create_app(service))
    response = client.post(
        "/api/search",
        json={"query": "Один взрослый поездом из Москвы в Санкт-Петербург"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scenarios"] == []
    assert "Туту не принял" in body["summary"]
    assert "pydantic.dev" not in json.dumps(body, ensure_ascii=False)
    assert [name for name, _arguments in fake.calls] == ["get_rail_instructions", "search_rail"]
    checkout = client.post(
        "/api/checkout",
        json={"run_id": body["run_id"], "component_ref": "scenario-1:ticket", "variant_id": "missing"},
    )
    assert checkout.status_code == 422

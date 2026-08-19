"""Deterministic offline full-cycle cases from PLAN.md §4.2.

Solo is ``plan_group_sync`` with one participant. Mixed modes go through
``search_{common_mode}`` and ``search_{feeder_mode}``. No sleep, no live
network, no 15-minute timeouts.

Injection via ``/api/search`` is already covered in ``test_security.py`` and
is not duplicated here. Playwright browser matrix is deferred: no Playwright
dependency, FastAPI TestClient covers API and HTML.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.app.application import build_live_service
from agent.app.config import Settings
from agent.app.models import AssistantTurn, ProviderMessage, ToolCall
from agent.app.tutu_mcp import FakeTutuMcpClient, TutuMcpGateway
from agent.app.web import create_app


COMMON_DEPARTURE = "2026-08-21T13:30:00+03:00"
COMMON_ARRIVAL = "2026-08-21T18:05:00+03:00"
FEEDER_DEPARTURE = "2026-08-21T06:10:00+03:00"
DATE = "2026-08-21"

AIR_FLOOR = 240
RAIL_FLOOR = 30
BUS_FLOOR = 45


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


def _arrival_before(wait_minutes: int) -> str:
    departure = datetime.fromisoformat(COMMON_DEPARTURE)
    return (departure - timedelta(minutes=wait_minutes)).isoformat()


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
    is_multi_pnr: bool = False,
) -> dict[str, Any]:
    ref = {
        "offer_hash": identifier,
        "passengers_full": passengers_full,
        "passengers_child": 0,
        "passengers_infant": 0,
        "provider_specific": {"preserve": identifier},
    }
    carrier = {"avia": "TK", "rail": "RZD", "bus": "BUS"}.get(mode, "XX")
    segment: dict[str, Any] = {
        "origin_code": origin,
        "destination_code": destination,
        "departure_at": departure,
        "arrival_at": arrival,
        "carrier": carrier,
        "mode": mode,
        "transport": mode,
    }
    if mode == "avia":
        segment["flight_number"] = identifier
    elif mode == "rail":
        segment["train_number"] = identifier
    else:
        segment["bus_number"] = identifier
    variant: dict[str, Any] = {
        "variant_id": f"{identifier}-flex",
        "offer_hash": f"{identifier}-fare-hash",
        "service_class": "ECONOMIC",
        "name": "Flex",
        "price": {"amount": price, "currency": "RUB"},
        "conditions": {"baggage": {"included": True, "pieces": 1}, "changeable": True},
    }
    # Aviation can refine the parent ref via offer_hash/service_class.
    # Rail/bus need an exact variant checkout_ref (no avia override).
    if mode != "avia":
        variant["checkout_ref"] = dict(ref)
    return {
        "id": identifier,
        "transport": mode,
        "mode": mode,
        "price": {"amount": price, "currency": "RUB"},
        "legs": [{"segments": [segment]}],
        "is_multi_pnr": is_multi_pnr,
        "conditions": {"baggage": {"included": True, "pieces": 1}},
        "variants": [variant],
        "checkout_ref": ref,
    }


def _plan_turn(
    *,
    common_mode: str,
    feeder_mode: str,
    contract: dict[str, Any],
) -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[
            ToolCall(
                id="plan-1",
                name="plan_group_sync",
                arguments={
                    "departure_date": DATE,
                    "common_mode": common_mode,
                    "feeder_mode": feeder_mode,
                    "contract": contract,
                },
            )
        ]
    )


def _contract(
    participants: Sequence[dict[str, Any]],
    *,
    hub: str,
    destination: str,
    min_wait: int = 0,
    max_wait: int = 24 * 60,
) -> dict[str, Any]:
    return {
        "participants": list(participants),
        "hub_code": hub,
        "destination_code": destination,
        "min_wait_minutes": min_wait,
        "max_wait_minutes": max_wait,
        "required_checked_baggage_pieces": 0,
        "strict_baggage": False,
    }


def _checkout_url(mode: str, identifier: str) -> str:
    return f"https://www.tutu.ru/{mode}/checkout/{identifier}"


def _build_stack(
    *,
    common_mode: str,
    feeder_mode: str,
    offers_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    contract: dict[str, Any],
    checkout: bool = True,
) -> tuple[Any, ScriptedLLM, FakeTutuMcpClient, dict[str, list[dict[str, Any]]]]:
    observed: dict[str, list[dict[str, Any]]] = {}

    def search(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        observed.setdefault(name, []).append(dict(arguments))
        pair = (arguments["origin"], arguments["destination"])
        offers = offers_by_pair.get(pair)
        if offers is None:
            raise AssertionError(f"{name} received unexpected pair {pair!r}")
        return {"offers": offers}

    responses: dict[str, Any] = {}
    for mode in {common_mode, feeder_mode}:
        responses[f"get_{mode}_instructions"] = {"text": f"{mode} search instructions"}
        responses[f"search_{mode}"] = search
    if checkout:
        responses["create_checkout_link"] = lambda _name, arguments: {
            "checkout_url": _checkout_url(common_mode, str(arguments.get("offer_hash") or "selected")),
            "kind": "checkout_deeplink",
            "observed_ref": arguments,
        }

    fake = FakeTutuMcpClient(responses)
    llm = ScriptedLLM([_plan_turn(common_mode=common_mode, feeder_mode=feeder_mode, contract=contract)])
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    return service, llm, fake, observed


def _client_for(service: Any) -> TestClient:
    return TestClient(create_app(service))


def _public_has_no_secrets(payload: dict[str, Any] | str) -> None:
    text = payload if isinstance(payload, str) else str(payload)
    assert "checkout_ref" not in text
    assert "provider_specific" not in text


def _tool_names(fake: FakeTutuMcpClient) -> list[str]:
    return [name for name, _arguments in fake.calls]


def _search(client: TestClient, query: str) -> tuple[Any, dict[str, Any]]:
    response = client.post("/api/search", json={"query": query})
    assert response.status_code == 200, response.text
    body = response.json()
    _public_has_no_secrets(response.text)
    return response, body


def _assert_blocked_no_cta(
    *,
    body: dict[str, Any],
    client: TestClient,
    fake: FakeTutuMcpClient,
    actual: int | None = None,
    required: int | None = None,
) -> None:
    scenarios = body.get("scenarios") or []
    for scenario in scenarios:
        assert scenario.get("safety_verdict") in {"blocked", "needs_verification", "caution"}
        assert scenario.get("booking_units") in ([], None)
        reason = str(scenario.get("safety_reason") or scenario.get("subtitle") or "")
        if actual is not None and required is not None and scenario.get("safety_verdict") == "blocked":
            assert str(actual) in reason
            assert str(required) in reason
            assert "нужен запас не меньше" in reason
    if not scenarios:
        assert body.get("scenarios") == []

    run_id = body["run_id"]
    checkout = client.post(
        "/api/checkout",
        json={
            "run_id": run_id,
            "component_ref": "scenario-1:common",
            "variant_id": "missing-flex",
        },
    )
    assert checkout.status_code == 422
    assert "create_checkout_link" not in _tool_names(fake)


def _assert_recommended_checkout(
    *,
    body: dict[str, Any],
    client: TestClient,
    fake: FakeTutuMcpClient,
    service: Any,
    expected_common_adults: int,
    participant_ids: Sequence[str],
) -> None:
    assert body["scenarios"]
    scenario = body["scenarios"][0]
    assert scenario["safety_verdict"] == "recommended"
    units = scenario["booking_units"]
    assert units
    common = next(unit for unit in units if unit["component_ref"] == "scenario-1:common")
    assert common["tariffs"]
    variant_id = common["tariffs"][0]["variant_id"]
    assert "отдельн" in common["scope"].lower() or "общ" in common["scope"].lower()
    for participant_id in participant_ids:
        feeder = next(
            unit for unit in units if unit["component_ref"] == f"scenario-1:feeder:{participant_id}"
        )
        assert feeder["tariffs"]
        assert "отдельн" in feeder["scope"].lower()

    stored = service.run_store.get(body["run_id"])
    assert stored.components
    common_variant = next(iter(stored.components["scenario-1:common"].variants.values()))
    assert common_variant.expected_passengers == {"passengers_full": expected_common_adults}

    handoff = client.post(
        "/api/checkout",
        json={
            "run_id": body["run_id"],
            "component_ref": "scenario-1:common",
            "variant_id": variant_id,
        },
    )
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["url"].startswith("https://www.tutu.ru/")
    assert _tool_names(fake)[-1] == "create_checkout_link"


def _offers_for(
    *,
    common_mode: str,
    feeder_mode: str,
    hub: str,
    destination: str,
    participants: Sequence[tuple[str, str, int, int]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    group_adults = sum(adults for _ident, _origin, adults, _wait in participants)
    mapping: dict[tuple[str, str], list[dict[str, Any]]] = {
        (hub, destination): [
            _offer(
                f"{common_mode}-common",
                origin=hub,
                destination=destination,
                departure=COMMON_DEPARTURE,
                arrival=COMMON_ARRIVAL,
                passengers_full=group_adults,
                price=30_000,
                mode=common_mode,
            )
        ]
    }
    for ident, origin, adults, wait in participants:
        mapping[(origin, hub)] = [
            _offer(
                f"{feeder_mode}-feeder-{ident}",
                origin=origin,
                destination=hub,
                departure=FEEDER_DEPARTURE,
                arrival=_arrival_before(wait),
                passengers_full=adults,
                price=11_000,
                mode=feeder_mode,
            )
        ]
    return mapping


@dataclass(frozen=True)
class CycleSpec:
    case_id: str
    common_mode: str
    feeder_mode: str
    participants: tuple[tuple[str, str, int], ...]
    hub: str
    destination: str
    happy_wait: int
    short_wait: int
    required_floor: int
    query: str


SOLO_SPECS = (
    CycleSpec("AIR_SOLO", "avia", "avia", (("Аня", "VKO", 1),), "IST", "LHR", 240, 119, AIR_FLOOR, "Одна Аня из VKO через IST в LHR 21 августа"),
    CycleSpec("RAIL_SOLO", "rail", "rail", (("Аня", "MOW", 1),), "LED", "SPB", 30, 20, RAIL_FLOOR, "Одна Аня поездом MOW через LED в SPB 21 августа"),
    CycleSpec("BUS_SOLO", "bus", "bus", (("Аня", "KZN", 1),), "MOW", "LED", 45, 40, BUS_FLOOR, "Одна Аня автобусом KZN через MOW в LED 21 августа"),
    CycleSpec("AIR_TO_AIR_SOLO", "avia", "avia", (("Аня", "VKO", 1),), "IST", "LHR", 240, 119, AIR_FLOOR, "Самолёт на самолёт: Аня VKO через IST в LHR"),
    CycleSpec("AIR_TO_RAIL_SOLO", "rail", "avia", (("Аня", "VKO", 1),), "IST", "LHR", 240, 119, AIR_FLOOR, "Самолёт на поезд: Аня VKO через IST в LHR"),
    CycleSpec("RAIL_TO_AIR_SOLO", "avia", "rail", (("Аня", "VKO", 1),), "IST", "LHR", 240, 119, AIR_FLOOR, "Поезд на самолёт: Аня VKO через IST в LHR"),
    CycleSpec("AIR_TO_BUS_SOLO", "bus", "avia", (("Аня", "VKO", 1),), "IST", "LHR", 240, 119, AIR_FLOOR, "Самолёт на автобус: Аня VKO через IST в LHR"),
    CycleSpec("BUS_TO_AIR_SOLO", "avia", "bus", (("Аня", "VKO", 1),), "IST", "LHR", 240, 119, AIR_FLOOR, "Автобус на самолёт: Аня VKO через IST в LHR"),
    CycleSpec("RAIL_TO_RAIL_SOLO", "rail", "rail", (("Аня", "MOW", 1),), "LED", "SPB", 30, 20, RAIL_FLOOR, "Поезд на поезд: Аня MOW через LED в SPB"),
    CycleSpec("BUS_TO_BUS_SOLO", "bus", "bus", (("Аня", "KZN", 1),), "MOW", "LED", 45, 40, BUS_FLOOR, "Автобус на автобус: Аня KZN через MOW в LED"),
)

GROUP_SPECS = (
    CycleSpec(
        "AIR_GROUP",
        "avia",
        "avia",
        (("Аня", "VKO", 2), ("Илья", "LED", 1)),
        "IST",
        "LHR",
        240,
        119,
        AIR_FLOOR,
        "Нас трое: Аня из VKO и Илья из LED через IST в LHR 21 августа",
    ),
    CycleSpec(
        "RAIL_GROUP",
        "rail",
        "rail",
        (("Аня", "MOW", 1), ("Илья", "KZN", 1)),
        "LED",
        "SPB",
        30,
        20,
        RAIL_FLOOR,
        "Нас двое поездом: Аня из MOW и Илья из KZN через LED в SPB",
    ),
    CycleSpec(
        "BUS_GROUP",
        "bus",
        "bus",
        (("Аня", "KZN", 1), ("Илья", "LED", 1)),
        "MOW",
        "SPB",
        45,
        40,
        BUS_FLOOR,
        "Нас двое автобусом: Аня из KZN и Илья из LED через MOW в SPB",
    ),
)


def _run_spec(spec: CycleSpec, *, wait: int, checkout: bool) -> tuple[Any, TestClient, FakeTutuMcpClient, dict[str, Any], dict[str, list[dict[str, Any]]]]:
    people = tuple((ident, origin, adults, wait) for ident, origin, adults in spec.participants)
    contract = _contract(
        [{"id": ident, "origin": origin, "adults": adults} for ident, origin, adults, _wait in people],
        hub=spec.hub,
        destination=spec.destination,
    )
    offers = _offers_for(
        common_mode=spec.common_mode,
        feeder_mode=spec.feeder_mode,
        hub=spec.hub,
        destination=spec.destination,
        participants=people,
    )
    service, _llm, fake, observed = _build_stack(
        common_mode=spec.common_mode,
        feeder_mode=spec.feeder_mode,
        offers_by_pair=offers,
        contract=contract,
        checkout=checkout,
    )
    client = _client_for(service)
    _response, body = _search(client, spec.query)
    return service, client, fake, body, observed


def _assert_mode_tools(fake: FakeTutuMcpClient, *, common_mode: str, feeder_mode: str, participant_count: int) -> None:
    names = _tool_names(fake)
    used = {common_mode, feeder_mode}
    for mode in used:
        assert f"get_{mode}_instructions" in names
        assert f"search_{mode}" in names
    assert names.count(f"search_{common_mode}") >= 1
    assert names.count(f"search_{feeder_mode}") >= participant_count


@pytest.mark.parametrize("spec", SOLO_SPECS + GROUP_SPECS, ids=lambda spec: spec.case_id)
def test_full_cycle_happy_path_has_tariff_and_handoff(spec: CycleSpec) -> None:
    service, client, fake, body, observed = _run_spec(spec, wait=spec.happy_wait, checkout=True)
    adults = sum(item[2] for item in spec.participants)
    _assert_mode_tools(
        fake,
        common_mode=spec.common_mode,
        feeder_mode=spec.feeder_mode,
        participant_count=len(spec.participants),
    )
    common_searches = observed[f"search_{spec.common_mode}"]
    def _party_count(item: dict[str, Any]) -> int | None:
        for key in ("adults", "passengers"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    assert any(_party_count(item) == adults and item["origin"] == spec.hub for item in common_searches)
    _assert_recommended_checkout(
        body=body,
        client=client,
        fake=fake,
        service=service,
        expected_common_adults=adults,
        participant_ids=[item[0] for item in spec.participants],
    )


@pytest.mark.parametrize("spec", SOLO_SPECS + GROUP_SPECS, ids=lambda spec: f"{spec.case_id}_short")
def test_full_cycle_short_buffer_blocks_cta(spec: CycleSpec) -> None:
    _service, client, fake, body, _observed = _run_spec(spec, wait=spec.short_wait, checkout=True)
    assert body["scenarios"]
    _assert_blocked_no_cta(
        body=body,
        client=client,
        fake=fake,
        actual=spec.short_wait,
        required=spec.required_floor,
    )


def test_connection_group_one_short_wait_blocks_recommendation() -> None:
    spec = CycleSpec(
        "CONNECTION_GROUP",
        "avia",
        "avia",
        (("Аня", "VKO", 1), ("Илья", "LED", 1)),
        "IST",
        "LHR",
        240,
        119,
        AIR_FLOOR,
        "Двое через IST в LHR, один фидер слишком короткий",
    )
    people = (("Аня", "VKO", 1, 119), ("Илья", "LED", 1, 240))
    contract = _contract(
        [{"id": ident, "origin": origin, "adults": adults} for ident, origin, adults, _wait in people],
        hub=spec.hub,
        destination=spec.destination,
    )
    offers = _offers_for(
        common_mode="avia",
        feeder_mode="avia",
        hub="IST",
        destination="LHR",
        participants=people,
    )
    service, _llm, fake, _observed = _build_stack(
        common_mode="avia",
        feeder_mode="avia",
        offers_by_pair=offers,
        contract=contract,
        checkout=True,
    )
    client = _client_for(service)
    _response, body = _search(client, spec.query)
    assert body["scenarios"]
    scenario = body["scenarios"][0]
    assert scenario["safety_verdict"] == "blocked"
    reason = str(scenario.get("safety_reason") or scenario.get("subtitle") or "")
    assert "119" in reason
    assert "240" in reason
    _assert_blocked_no_cta(body=body, client=client, fake=fake, actual=119, required=240)


def test_rail_to_rail_different_station_is_not_recommended() -> None:
    """Solver excludes a feeder that arrives at another station before SafetyGate.

    Live ``plan_group_sync`` never presents that combination as a scenario, so
    there is no CTA. The policy-level ``needs_verification`` wording for a
    different station is covered in ``test_safety.py``.
    """

    contract = _contract([{"id": "Аня", "origin": "MOW", "adults": 1}], hub="LED", destination="SPB")
    offers = {
        ("LED", "SPB"): [
            _offer(
                "rail-common",
                origin="LED",
                destination="SPB",
                departure=COMMON_DEPARTURE,
                arrival=COMMON_ARRIVAL,
                passengers_full=1,
                price=8_000,
                mode="rail",
            )
        ],
        ("MOW", "LED"): [
            _offer(
                "rail-feeder-wrong-hub",
                origin="MOW",
                destination="SAW",
                departure=FEEDER_DEPARTURE,
                arrival=_arrival_before(120),
                passengers_full=1,
                price=4_000,
                mode="rail",
            )
        ],
    }
    service, _llm, fake, _observed = _build_stack(
        common_mode="rail",
        feeder_mode="rail",
        offers_by_pair=offers,
        contract=contract,
        checkout=True,
    )
    client = _client_for(service)
    _response, body = _search(client, "Поезд MOW с пересадкой не на той станции LED")
    recommended = [item for item in body.get("scenarios") or [] if item.get("safety_verdict") == "recommended"]
    assert recommended == []
    text = f"{body.get('summary') or ''} {body.get('rejection_summary') or ''}".lower()
    assert "точк" in text or "встреч" in text
    _assert_blocked_no_cta(body=body, client=client, fake=fake)


def test_air_solo_html_shows_tariff_radio_and_blocked_hides_cta() -> None:
    happy = SOLO_SPECS[0]
    people = (("Аня", "VKO", 1, happy.happy_wait),)
    contract = _contract([{"id": "Аня", "origin": "VKO", "adults": 1}], hub="IST", destination="LHR")
    offers = _offers_for(common_mode="avia", feeder_mode="avia", hub="IST", destination="LHR", participants=people)
    service, _llm, _fake, _obs = _build_stack(
        common_mode="avia",
        feeder_mode="avia",
        offers_by_pair=offers,
        contract=contract,
        checkout=True,
    )
    client = _client_for(service)
    html = client.post("/search", data={"query": happy.query})
    assert html.status_code == 200
    assert "checkout_ref" not in html.text
    assert "offer-link" in html.text
    assert "data-variant-id=" in html.text
    assert "↗" in html.text
    assert "Выберите точный тариф" not in html.text
    assert 'name="tariff-' not in html.text
    assert "Получить ссылку на Туту" not in html.text

    short_people = (("Аня", "VKO", 1, 119),)
    short_offers = _offers_for(
        common_mode="avia",
        feeder_mode="avia",
        hub="IST",
        destination="LHR",
        participants=short_people,
    )
    blocked_service, _llm2, fake2, _obs2 = _build_stack(
        common_mode="avia",
        feeder_mode="avia",
        offers_by_pair=short_offers,
        contract=contract,
        checkout=True,
    )
    blocked_html = _client_for(blocked_service).post("/search", data={"query": happy.query})
    assert blocked_html.status_code == 200
    assert "checkout_ref" not in blocked_html.text
    assert "Выберите точный тариф" not in blocked_html.text
    assert 'name="tariff-' not in blocked_html.text
    assert "offer-link" not in blocked_html.text
    assert "119" in blocked_html.text
    assert "240" in blocked_html.text
    assert "create_checkout_link" not in _tool_names(fake2)


def test_multi_pnr_caution_has_no_checkout() -> None:
    contract = _contract([{"id": "Аня", "origin": "VKO", "adults": 1}], hub="IST", destination="LHR")
    offers = _offers_for(
        common_mode="avia",
        feeder_mode="avia",
        hub="IST",
        destination="LHR",
        participants=(("Аня", "VKO", 1, 240),),
    )
    offers[("IST", "LHR")][0]["is_multi_pnr"] = True
    service, _llm, fake, _observed = _build_stack(
        common_mode="avia",
        feeder_mode="avia",
        offers_by_pair=offers,
        contract=contract,
        checkout=True,
    )
    client = _client_for(service)
    _response, body = _search(client, "Аня из VKO через IST в LHR отдельными билетами")
    assert body["scenarios"]
    assert body["scenarios"][0]["safety_verdict"] == "caution"
    _assert_blocked_no_cta(body=body, client=client, fake=fake)


def test_empty_tutu_offers_have_no_cta() -> None:
    contract = _contract([{"id": "Аня", "origin": "VKO", "adults": 1}], hub="IST", destination="LHR")
    service, _llm, fake, _observed = _build_stack(
        common_mode="avia",
        feeder_mode="avia",
        offers_by_pair={("IST", "LHR"): [], ("VKO", "IST"): []},
        contract=contract,
        checkout=True,
    )
    client = _client_for(service)
    _response, body = _search(client, "Аня из VKO через IST в LHR без офферов")
    assert body["scenarios"] == []
    _assert_blocked_no_cta(body=body, client=client, fake=fake)

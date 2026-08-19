"""End-to-end offline proof of the live composition root.

This test deliberately exercises the same boundaries as the browser: a model
chooses a tool, Python calls fake Tutu MCP, the result is turned into cards,
and a later explicit variant selection creates exactly one handoff link.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Sequence
from typing import Any

import pytest

from agent.app.application import build_live_service
from agent.app.config import Settings
from agent.app.models import AssistantTurn, ProviderMessage, ToolCall
from agent.app.presentation import _component_from_raw_offer
from agent.app.service import (
    CheckoutComponent,
    CheckoutSelectionError,
    CheckoutVariant,
    ConcessionSelectionError,
    RunStore,
)
from agent.app.tutu_mcp import FakeTutuMcpClient, TutuMcpGateway


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
        "transport": "avia",
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
                # Live aviation replies commonly identify a selected fare by
                # an offer hash distinct from the parent search-result id.
                "offer_hash": f"{identifier}-fare-hash",
                "service_class": "ECONOMIC",
                "name": "Flex",
                "price": {"amount": price, "currency": "RUB"},
                "conditions": {"baggage": {"included": True, "pieces": 1}, "changeable": True},
            }
        ],
        "checkout_ref": ref,
    }


def test_live_agent_tool_call_builds_cards_and_creates_selected_tutu_handoff() -> None:
    common = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=2,
        price=30000,
    )
    # The parent offer also exposes a cheaper fare without checked baggage.
    # With strict baggage it must never leak back into the scenario card after
    # the deterministic solver selected Flex.
    common["variants"].insert(
        0,
        {
            "variant_id": "TK1983-light",
            "offer_hash": "TK1983-light-hash",
            "service_class": "ECONOMIC",
            "name": "Light",
            "price": {"amount": 25_000, "currency": "RUB"},
            "conditions": {"baggage": {"included": False, "pieces": 0}, "changeable": False},
        },
    )
    moscow = _offer(
        "TK101",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:10:00+03:00",
        arrival="2026-08-21T09:30:00+03:00",
        passengers_full=1,
        price=11000,
    )
    petersburg = _offer(
        "TK102",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T09:20:00+03:00",
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
                "checkout_url": "https://www.tutu.ru/avia/checkout/TK1983?opaque=%2Fkeep",
                "kind": "checkout_deeplink",
                "observed_ref": arguments,
            },
        }
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="plan_group_sync",
                        arguments={
                            "departure_date": "2026-08-21",
                            "common_mode": "avia",
                            "contract": {
                                "participants": [
                                    {"id": "Аня", "origin": "VKO"},
                                    {"id": "Илья", "origin": "LED"},
                                ],
                                "hub_code": "IST",
                                "destination_code": "LHR",
                                "min_wait_minutes": 120,
                                "max_wait_minutes": 300,
                                "required_checked_baggage_pieces": 1,
                                "strict_baggage": True,
                            },
                        },
                    )
                ]
            )
        ]
    )
    settings = Settings(openrouter_api_key="test", agent_max_steps=4)
    service = build_live_service(settings=settings, tutu=TutuMcpGateway(fake), llm_factory=lambda: llm)

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        result = await service.run("Нас двое через IST в LHR 21 августа")
        stored = service.run_store.get(result["run_id"])
        assert stored.components, repr(result["scenarios"][0]["booking_units"])
        assert stored.components["scenario-1:common"].variants["TK1983-flex"].expected_passengers == {
            "passengers_full": 2
        }
        assert stored.components["scenario-1:feeder:Аня"].variants["TK101-flex"].expected_passengers == {
            "passengers_full": 1
        }
        handoff = await service.create_checkout_link(result["run_id"], "scenario-1:common", "TK1983-flex")
        return result, handoff

    result, handoff = asyncio.run(exercise())

    assert result["scenarios"]
    assert result["scenarios"][0]["booking_units"]
    common_unit = next(
        unit
        for unit in result["scenarios"][0]["booking_units"]
        if unit["component_ref"] == "scenario-1:common"
    )
    assert [tariff["variant_id"] for tariff in common_unit["tariffs"]] == ["TK1983-flex"]
    public_json = json.dumps(result, ensure_ascii=False)
    assert "checkout_ref" not in public_json
    assert "provider_specific" not in public_json
    assert handoff["url"] == "https://www.tutu.ru/avia/checkout/TK1983?opaque=%2Fkeep"
    selected_common_variant = next(item for item in common["variants"] if item["variant_id"] == "TK1983-flex")
    expected_common_ref = {
        **common["checkout_ref"],
        "offer_hash": selected_common_variant["offer_hash"],
        "service_class": selected_common_variant["service_class"],
    }
    assert fake.calls[-1] == ("create_checkout_link", expected_common_ref)

    # The LLM was given the bounded group planner, not an implicit hard-coded
    # route.  The successful high-level tool is terminal: it supplies the
    # deterministic presentation directly instead of triggering a redundant
    # second model turn and another round of searches.
    assert [schema["function"]["name"] for schema in llm.calls[0][1] if schema["function"]["name"] == "plan_group_sync"] == ["plan_group_sync"]
    assert len(llm.calls) == 1
    assert [call[0] for call in fake.calls[:4]] == [
        "get_avia_instructions",
        "search_avia",
        "search_avia",
        "search_avia",
    ]


def test_live_agent_hides_common_handoff_when_tutu_ref_has_wrong_group_size() -> None:
    """A two-person group must never receive a checkout ref for one adult."""

    common = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=1,
        price=30000,
    )
    moscow = _offer(
        "TK101",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T06:10:00+03:00",
        arrival="2026-08-21T09:30:00+03:00",
        passengers_full=1,
        price=11000,
    )
    petersburg = _offer(
        "TK102",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T09:30:00+03:00",
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
            "create_checkout_link": {"checkout_url": "https://www.tutu.ru/should-not-be-called"},
        }
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="plan_group_sync",
                        arguments={
                            "departure_date": "2026-08-21",
                            "common_mode": "avia",
                            "contract": {
                                "participants": [
                                    {"id": "Аня", "origin": "VKO"},
                                    {"id": "Илья", "origin": "LED"},
                                ],
                                "hub_code": "IST",
                                "destination_code": "LHR",
                                "min_wait_minutes": 120,
                                "max_wait_minutes": 300,
                            },
                        },
                    )
                ]
            ),
            AssistantTurn(content="Проверил сценарий.", finish_reason="stop"),
        ]
    )
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )

    result = asyncio.run(service.run("Нас двое через IST в LHR 21 августа"))

    common_unit = next(
        unit
        for unit in result["scenarios"][0]["booking_units"]
        if unit["component_ref"] == "scenario-1:common"
    )
    assert common_unit["tariffs"] == []
    assert "другого числа пассажиров" in common_unit["handoff_note"]
    assert "scenario-1:common" not in service.run_store.get(result["run_id"]).components

    with pytest.raises(CheckoutSelectionError):
        asyncio.run(service.create_checkout_link(result["run_id"], "scenario-1:common", "TK1983-flex"))
    assert all(name != "create_checkout_link" for name, _arguments in fake.calls)


def test_presentation_never_turns_a_generic_offer_ref_into_a_variant_checkout() -> None:
    """A generic search ref must not substitute for a mismatched fare ref."""

    raw = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=2,
        price=30_000,
    )
    raw["checkout_ref"] = {"offer_hash": "generic-search-ref", "passengers_full": 2}
    # Exact returned fare is for another party size; the generic parent ref
    # must not silently become a purchasable replacement.
    raw["variants"][0]["checkout_ref"] = {"offer_hash": "fare-for-one", "passengers_full": 1}

    unit, private = _component_from_raw_offer(
        raw,
        component_ref="scenario-1:common",
        title="Общее плечо",
        scope="Общее",
        expected_passengers={"passengers_full": 2},
    )

    assert unit is not None
    assert unit["tariffs"] == []
    assert private is None


def test_live_agent_surfaces_a_safe_one_concession_without_a_fare_or_ref() -> None:
    """The add-on may explain one verified relaxation, never resurrect a fare."""

    common = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=2,
        price=30_000,
    )
    # This is 340 minutes before the common flight: exactly 40 minutes above
    # the requested maximum, while the other participant already fits.
    moscow = _offer(
        "TK101",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T04:00:00+03:00",
        arrival="2026-08-21T07:50:00+03:00",
        passengers_full=1,
        price=11_000,
    )
    petersburg = _offer(
        "TK102",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        # Keep the already-valid feeder outside the <4h structural-risk zone;
        # this test is about the sole upper-window concession.
        arrival="2026-08-21T09:20:00+03:00",
        passengers_full=1,
        price=12_000,
    )

    def search_avia(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        pair = (arguments["origin"], arguments["destination"])
        offers = {("IST", "LHR"): [common], ("VKO", "IST"): [moscow], ("LED", "IST"): [petersburg]}[pair]
        return {"offers": offers}

    fake = FakeTutuMcpClient(
        {"get_avia_instructions": {"text": "search instructions"}, "search_avia": search_avia}
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="plan_group_sync",
                        arguments={
                            "departure_date": "2026-08-21",
                            "contract": {
                                "participants": [
                                    {"id": "Аня", "origin": "VKO"},
                                    {"id": "Илья", "origin": "LED"},
                                ],
                                "hub_code": "IST",
                                "destination_code": "LHR",
                                "min_wait_minutes": 120,
                                "max_wait_minutes": 300,
                            },
                        },
                    )
                ]
            ),
            AssistantTurn(content="Есть только одна безопасная уступка.", finish_reason="stop"),
        ]
    )
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )

    result = asyncio.run(service.run("Нас двое через IST в LHR 21 августа"))

    assert result["scenarios"] == []
    assert result["constraint_negotiator"] == {
        "kind": "increase_max_wait",
        "from_max_wait_minutes": 300,
        "to_max_wait_minutes": 340,
        "delta_minutes": 40,
        "affected_participants": [{"participant_id": "Аня", "wait_minutes": 340}],
        "trigger": {"participant_id": "Аня", "observed_wait_minutes": 340},
        "verified": {
            "baseline_scenarios": 0,
            "rerun_scenarios": 1,
            "exact_hub_preserved": True,
            "common_segment_preserved": True,
            "risk_status": "pass",
        },
    }
    public_json = json.dumps(result, ensure_ascii=False)
    assert "checkout_ref" not in public_json
    assert "TK101" not in public_json
    assert not service.run_store.get(result["run_id"]).components


def test_safe_concession_replan_uses_only_the_server_recipe_and_fresh_tutu_search() -> None:
    """A click can change only the exact offered upper bound, never the plan."""

    common = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
        passengers_full=3,
        price=30_000,
    )
    moscow = _offer(
        "TK101",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T04:00:00+03:00",
        arrival="2026-08-21T07:50:00+03:00",  # 340 min before the common leg
        passengers_full=2,
        price=11_000,
    )
    petersburg = _offer(
        "TK102",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        arrival="2026-08-21T09:20:00+03:00",
        passengers_full=1,
        price=12_000,
    )
    search_arguments: list[dict[str, Any]] = []

    def search_avia(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        search_arguments.append(copy.deepcopy(arguments))
        pair = (arguments["origin"], arguments["destination"])
        offers = {("IST", "LHR"): [common], ("VKO", "IST"): [moscow], ("LED", "IST"): [petersburg]}[pair]
        return {"offers": offers}

    fake = FakeTutuMcpClient(
        {"get_avia_instructions": {"text": "search instructions"}, "search_avia": search_avia}
    )
    llm = ScriptedLLM(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="plan_group_sync",
                        arguments={
                            "departure_date": "2026-08-21",
                            "common_mode": "avia",
                            "feeder_mode": "avia",
                            "contract": {
                                "participants": [
                                    {"id": "Аня", "origin": "VKO", "adults": 2},
                                    {"id": "Илья", "origin": "LED", "adults": 1},
                                ],
                                "hub_code": "IST",
                                "destination_code": "LHR",
                                "min_wait_minutes": 120,
                                "max_wait_minutes": 300,
                                "required_checked_baggage_pieces": 0,
                                "strict_baggage": False,
                            },
                        },
                    )
                ]
            ),
            AssistantTurn(content="Есть безопасная уступка.", finish_reason="stop"),
        ]
    )
    service = build_live_service(
        settings=Settings(openrouter_api_key="test", agent_max_steps=4),
        tutu=TutuMcpGateway(fake),
        llm_factory=lambda: llm,
    )
    solve_arguments: list[dict[str, Any]] = []
    original_solve = service.solve_group_rendezvous

    def recording_solve(arguments: dict[str, Any]) -> dict[str, Any]:
        solve_arguments.append(copy.deepcopy(arguments))
        return original_solve(arguments)

    service.solve_group_rendezvous = recording_solve  # type: ignore[method-assign]

    async def exercise() -> tuple[dict[str, Any], dict[str, Any], int]:
        baseline = await service.run("Нас трое через IST в LHR 21 августа")
        assert baseline["scenarios"] == []
        assert "constraint_negotiator" in baseline, baseline["rejection_summary"]
        baseline_store = service.run_store.get(baseline["run_id"])
        assert baseline_store.concession_replan_context is not None
        before_rejected_calls = len(search_arguments)
        with pytest.raises(ConcessionSelectionError):
            await service.replan_concession(baseline["run_id"], 339)
        with pytest.raises(ConcessionSelectionError):
            await service.replan_concession("foreign-run", 340)
        assert len(search_arguments) == before_rejected_calls
        recalculated = await service.replan_concession(baseline["run_id"], 340)
        return baseline, recalculated, before_rejected_calls

    baseline, recalculated, before_rejected_calls = asyncio.run(exercise())

    assert recalculated["run_id"] != baseline["run_id"]
    assert recalculated["scenarios"]
    assert service.run_store.get(recalculated["run_id"]).components
    # The first agent session stops after the terminal planner tool.  A safe
    # replan consumes no model turn and therefore cannot let the model alter
    # the stored recipe.
    assert len(llm.calls) == 1

    assert len(solve_arguments) == 2
    before_contract = solve_arguments[0]["contract"]
    after_contract = solve_arguments[1]["contract"]
    assert before_contract["max_wait_minutes"] == 300
    assert after_contract["max_wait_minutes"] == 340
    assert {key: value for key, value in after_contract.items() if key != "max_wait_minutes"} == {
        key: value for key, value in before_contract.items() if key != "max_wait_minutes"
    }

    # Every fresh MCP request repeats the same routes, date and cardinality.
    assert search_arguments[:before_rejected_calls] == [
        {"origin": "IST", "destination": "LHR", "departure_date": "2026-08-21", "adults": 3, "view": "compact"},
        {"origin": "VKO", "destination": "IST", "departure_date": "2026-08-21", "adults": 2, "view": "compact"},
        {"origin": "LED", "destination": "IST", "departure_date": "2026-08-21", "adults": 1, "view": "compact"},
    ]
    assert search_arguments[before_rejected_calls:] == search_arguments[:before_rejected_calls]
    public_json = json.dumps(recalculated, ensure_ascii=False)
    assert "concession_replan_context" not in public_json
    assert "checkout_ref" not in public_json


def test_run_store_supersedes_old_selection_only_after_atomic_concession_commit() -> None:
    """A successful fresh result closes the old checkout/replan surface.

    This is deliberately a RunStore-level test: it covers the ordering around
    an external MCP call without needing to make an artificial checkout
    request.  A failed replan rolls the claim back; a committed one atomically
    publishes a forced-new run ID and supersedes the source.
    """

    store = RunStore()
    variant = CheckoutVariant(
        variant_id="fare-1",
        checkout_ref={"offer_hash": "opaque-ref"},
        offer_snapshot={"id": "offer-1"},
        expected_passengers={"passengers_full": 1},
    )
    component = CheckoutComponent("scenario-1:common", {variant.variant_id: variant})
    proposal = {
        "kind": "increase_max_wait",
        "from_max_wait_minutes": 300,
        "to_max_wait_minutes": 340,
    }
    context = {
        "contract": {
            "participants": [{"id": "Аня", "origin_code": "VKO", "adults": 1}],
            "hub_code": "IST",
            "destination_code": "LHR",
            "min_wait_minutes": 120,
            "max_wait_minutes": 300,
        },
        "departure_date": "2026-08-21",
        "common_mode": "avia",
        "feeder_mode": "avia",
        "query": "Исходный запрос",
        "proposed_max_wait_minutes": 340,
    }
    source = store.put(
        {"run_id": "source-run", "constraint_negotiator": proposal, "scenarios": []},
        [component],
        concession_replan_context=context,
    )

    # An in-flight handoff prevents a replan from racing it.  Once that
    # handoff is released, a failed fresh search restores the original run.
    store.claim_variant(source.run_id, component.component_ref, variant.variant_id)
    with pytest.raises(ConcessionSelectionError):
        store.begin_concession_replan(source.run_id, 340)
    store.release_variant_claim(source.run_id)

    _, recipe = store.begin_concession_replan(source.run_id, 340)
    assert recipe["proposed_max_wait_minutes"] == 340
    with pytest.raises(CheckoutSelectionError):
        store.resolve_variant(source.run_id, component.component_ref, variant.variant_id)
    with pytest.raises(ConcessionSelectionError):
        store.resolve_concession_replan(source.run_id, 340)
    store.abort_concession_replan(source.run_id)
    assert store.resolve_variant(source.run_id, component.component_ref, variant.variant_id)[2] is variant
    assert store.resolve_concession_replan(source.run_id, 340)[1]["proposed_max_wait_minutes"] == 340

    # Even a payload that repeats the old run id cannot revive it: commit
    # generates a fresh run and then supersedes the source under one lock.
    store.begin_concession_replan(source.run_id, 340)
    replacement = store.commit_concession_replan(
        source.run_id,
        {"run_id": source.run_id, "summary": "Свежий расчёт", "scenarios": []},
        [component],
    )
    assert replacement.run_id != source.run_id
    assert store.resolve_variant(replacement.run_id, component.component_ref, variant.variant_id)[2] is variant
    with pytest.raises(CheckoutSelectionError):
        store.resolve_variant(source.run_id, component.component_ref, variant.variant_id)
    with pytest.raises(ConcessionSelectionError):
        store.resolve_concession_replan(source.run_id, 340)

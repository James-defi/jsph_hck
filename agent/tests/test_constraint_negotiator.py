"""Tests for the safe, deterministic one-concession helper."""

from __future__ import annotations

import json

import pytest

from agent.app.constraint_negotiator import suggest_one_max_wait_concession
from agent.app.solver import solve_group_rendezvous


def _offer(
    identifier: str,
    *,
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    is_multi_pnr: bool = False,
) -> dict:
    return {
        "id": identifier,
        "mode": "avia",
        "is_multi_pnr": is_multi_pnr,
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
        # It must never leave this helper's public result; keeping it in test
        # inputs proves the module does not accidentally leak a checkout path.
        "checkout_ref": {"offer_hash": identifier, "passengers_full": 1},
    }


def _contract() -> dict:
    return {
        "participants": [{"id": "moscow", "origin": "VKO"}, {"id": "petersburg", "origin": "LED"}],
        "hub_code": "IST",
        "destination_code": "LHR",
        "min_wait_minutes": 120,
        "max_wait_minutes": 300,
    }


def _base_inputs(*, moscow_arrival: str = "2026-08-21T07:50:00+03:00", moscow_multi_pnr: bool = False) -> tuple[dict, list[dict], dict[str, list[dict]]]:
    common = _offer(
        "TK1983",
        origin="IST",
        destination="LHR",
        departure="2026-08-21T13:30:00+03:00",
        arrival="2026-08-21T16:05:00+01:00",
    )
    moscow = _offer(
        "TK101",
        origin="VKO",
        destination="IST",
        departure="2026-08-21T04:00:00+03:00",
        arrival=moscow_arrival,
        is_multi_pnr=moscow_multi_pnr,
    )
    petersburg = _offer(
        "TK102",
        origin="LED",
        destination="IST",
        departure="2026-08-21T06:00:00+03:00",
        # 250 minutes: comfortably above the 4h structural warning threshold
        # so the separate one-concession test isolates only max_wait.
        arrival="2026-08-21T09:20:00+03:00",
    )
    return _contract(), [common], {"moscow": [moscow], "petersburg": [petersburg]}


def test_proposes_the_smallest_safe_increase_from_real_solver_exclusion() -> None:
    contract, common, feeders = _base_inputs()
    # Moscow waits 340 minutes.  The original maximum is 300; Petersburg is
    # already within the window.  A second real option needs 360 minutes, so
    # a 40-minute increase is the exact minimum rather than merely *an*
    # increase that happens to work.
    feeders["moscow"].append(
        _offer(
            "TK103",
            origin="VKO",
            destination="IST",
            departure="2026-08-21T04:00:00+03:00",
            arrival="2026-08-21T07:30:00+03:00",
        )
    )
    baseline = solve_group_rendezvous(contract, common, feeders)

    proposal = suggest_one_max_wait_concession(
        contract=contract,
        solver_result=baseline,
        common_offers=common,
        feeders_by_participant=feeders,
    )

    assert baseline["scenarios"] == []
    assert baseline["excluded"][0]["common_offer_id"] == "TK1983"
    assert proposal == {
        "kind": "increase_max_wait",
        "from_max_wait_minutes": 300,
        "to_max_wait_minutes": 340,
        "delta_minutes": 40,
        "affected_participants": [{"participant_id": "moscow", "wait_minutes": 340}],
        "trigger": {"participant_id": "moscow", "observed_wait_minutes": 340},
        "verified": {
            "baseline_scenarios": 0,
            "rerun_scenarios": 1,
            "exact_hub_preserved": True,
            "common_segment_preserved": True,
            "risk_status": "pass",
        },
    }
    public_json = json.dumps(proposal, ensure_ascii=False)
    assert "checkout_ref" not in public_json
    assert "TK101" not in public_json


def test_never_weakens_minimum_wait_even_if_that_would_create_a_scenario() -> None:
    # The only miss is a 60-minute connection.  This helper deliberately
    # refuses to trade away the lower safety buffer.
    contract, common, feeders = _base_inputs(moscow_arrival="2026-08-21T12:30:00+03:00")
    baseline = solve_group_rendezvous(contract, common, feeders)

    proposal = suggest_one_max_wait_concession(
        contract=contract,
        solver_result=baseline,
        common_offers=common,
        feeders_by_participant=feeders,
    )

    assert baseline["scenarios"] == []
    assert baseline["excluded_summary"] == {"outside_meeting_window": 1, "no_feeder_within_exact_hub_window": 1}
    assert proposal is None


def test_refuses_a_recovered_scenario_that_still_has_a_risk() -> None:
    contract, common, feeders = _base_inputs(moscow_multi_pnr=True)
    baseline = solve_group_rendezvous(contract, common, feeders)

    proposal = suggest_one_max_wait_concession(
        contract=contract,
        solver_result=baseline,
        common_offers=common,
        feeders_by_participant=feeders,
    )

    # Raising the upper bound would make the times work, but self-transfer is
    # still a risk; this is for the separate "do not buy" layer, not a safe
    # one-concession card.
    assert baseline["scenarios"] == []
    assert proposal is None


def test_does_not_offer_a_concession_when_a_baseline_scenario_already_exists() -> None:
    contract, common, feeders = _base_inputs(moscow_arrival="2026-08-21T10:10:00+03:00")
    baseline = solve_group_rendezvous(contract, common, feeders)

    assert baseline["scenarios"]
    assert (
        suggest_one_max_wait_concession(
            contract=contract,
            solver_result=baseline,
            common_offers=common,
            feeders_by_participant=feeders,
        )
        is None
    )


def test_rejects_an_unbounded_candidate_target_configuration() -> None:
    contract, common, feeders = _base_inputs()
    baseline = solve_group_rendezvous(contract, common, feeders)

    with pytest.raises(ValueError, match="max_candidate_targets"):
        suggest_one_max_wait_concession(
            contract=contract,
            solver_result=baseline,
            common_offers=common,
            feeders_by_participant=feeders,
            max_candidate_targets=0,
        )

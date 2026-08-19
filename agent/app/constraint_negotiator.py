"""One-safe-concession proposals over an already trusted GroupSync search.

This module deliberately does *not* search Tutu, change a stored run, create
checkout links, or expose source offers.  Its caller supplies MCP-trusted
offers plus the baseline result made by :class:`GroupSyncSolver`; the function
only returns a small, public explanation of one verified change to the maximum
waiting-time preference.

The lower waiting boundary is a safety buffer and is never weakened here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .geo import meeting_point_matches
from .solver import GroupSyncSolver, GroupTripContract, STATUS_PASS, TravelOffer


JSON = dict[str, Any]
TrustedOffer = Mapping[str, Any] | TravelOffer
DEFAULT_MAX_CANDIDATE_TARGETS = 8


def suggest_one_max_wait_concession(
    *,
    contract: GroupTripContract | Mapping[str, Any],
    solver_result: Mapping[str, Any],
    common_offers: Iterable[TrustedOffer],
    feeders_by_participant: Mapping[str, Iterable[TrustedOffer]],
    solver: GroupSyncSolver | None = None,
    max_candidate_targets: int = DEFAULT_MAX_CANDIDATE_TARGETS,
) -> JSON | None:
    """Return the smallest safe increase of ``max_wait_minutes``, if any.

    The function fails closed.  It emits a proposal only when all of these are
    true:

    * the original deterministic search returned *zero* scenarios;
    * an actual baseline exclusion says a feeder exceeded exactly this
      contract's maximum wait for a particular common offer;
    * re-running the solver with only a larger maximum creates a scenario that
      uses that exact feeder/common-offer pair;
    * the re-run scenario preserves the exact hub and direct shared segment,
      contains every participant, and has no ``risk`` or ``unknown`` finding.

    ``common_offers`` and ``feeders_by_participant`` must already be trusted
    MCP snapshots.  This pure helper cannot establish their provenance itself.
    The returned mapping intentionally contains no raw offer, price, checkout
    reference, URL, or mutable contract patch.
    """

    if max_candidate_targets < 1:
        raise ValueError("max_candidate_targets must be at least 1")
    contract_value = contract if isinstance(contract, GroupTripContract) else GroupTripContract.from_mapping(contract)
    baseline_scenarios = _sequence(solver_result.get("scenarios"))
    if baseline_scenarios is None or baseline_scenarios:
        return None

    exclusions = _sequence(solver_result.get("excluded"))
    if exclusions is None:
        return None

    # Preserve the exact caller snapshots across several deterministic re-runs
    # without serialising or modifying them.
    common_snapshot = tuple(common_offers)
    feeder_snapshot = {participant_id: tuple(offers) for participant_id, offers in feeders_by_participant.items()}
    candidate_exclusions = _eligible_max_wait_exclusions(exclusions, contract_value)
    if not candidate_exclusions:
        return None

    active_solver = solver or GroupSyncSolver()
    by_target: dict[int, list[JSON]] = {}
    for exclusion in candidate_exclusions:
        by_target.setdefault(int(exclusion["wait_minutes"]), []).append(exclusion)

    # Targets are actual observed waits.  There is no rounding: suggesting
    # 360 minutes for a real 355-minute wait would silently make a larger
    # concession than the data requires.
    # A pathological no-result search may yield hundreds of distinct wait
    # values.  Re-running the combinatorial solver for every one would turn a
    # helpful optional card into an unbounded request.  Check only the first
    # few *smallest* real concessions; this keeps the feature conservative.
    for target_max_wait in sorted(by_target)[:max_candidate_targets]:
        amended_contract = contract_value.to_dict()
        amended_contract["max_wait_minutes"] = target_max_wait
        rerun = active_solver.solve(
            amended_contract,
            common_snapshot,
            feeder_snapshot,
            max_scenarios=1,
        )
        rerun_scenarios = _sequence(rerun.get("scenarios"))
        if not rerun_scenarios:
            continue

        for scenario in rerun_scenarios:
            if not isinstance(scenario, Mapping):
                continue
            if not _safe_exact_scenario(scenario, contract_value, target_max_wait):
                continue
            matching = _matching_exclusion(scenario, by_target[target_max_wait])
            if matching is None:
                continue
            return _public_proposal(
                scenario=scenario,
                matched_exclusion=matching,
                original_max_wait=contract_value.max_wait_minutes,
                proposed_max_wait=target_max_wait,
            )

    return None


def _eligible_max_wait_exclusions(
    exclusions: Sequence[Any],
    contract: GroupTripContract,
) -> list[JSON]:
    """Select only well-scoped, observed breaches of the upper wait bound."""

    eligible: list[JSON] = []
    for item in exclusions:
        if not isinstance(item, Mapping):
            continue
        if item.get("reason") != "outside_meeting_window":
            continue
        try:
            wait = int(item["wait_minutes"])
            minimum = int(item["min_wait_minutes"])
            maximum = int(item["max_wait_minutes"])
        except (KeyError, TypeError, ValueError):
            continue
        # The exclusion must come from this exact baseline contract.  This
        # rejects stale, model-supplied, or lower-bound violations.
        if minimum != contract.min_wait_minutes or maximum != contract.max_wait_minutes:
            continue
        if wait <= maximum:
            continue
        participant_id = str(item.get("participant_id") or "").strip()
        offer_id = str(item.get("offer_id") or "").strip()
        common_offer_id = str(item.get("common_offer_id") or "").strip()
        if not participant_id or not offer_id or not common_offer_id:
            continue
        eligible.append(
            {
                "participant_id": participant_id,
                "offer_id": offer_id,
                "common_offer_id": common_offer_id,
                "wait_minutes": wait,
            }
        )
    return eligible


def _safe_exact_scenario(
    scenario: Mapping[str, Any],
    contract: GroupTripContract,
    proposed_max_wait: int,
) -> bool:
    """Independently re-check the facts the solver is expected to preserve."""

    risks = scenario.get("risks")
    if not isinstance(risks, Mapping) or risks.get("overall_status") != STATUS_PASS:
        return False
    findings = _sequence(risks.get("findings"))
    if findings is None or any(not isinstance(item, Mapping) or item.get("status") != STATUS_PASS for item in findings):
        return False

    signature = scenario.get("common_service_signature")
    if not isinstance(signature, Mapping):
        return False
    signature_mode = str(signature.get("mode") or "")
    if not meeting_point_matches(
        contract.hub_code, code=str(signature.get("origin_code") or ""), mode=signature_mode
    ):
        return False

    common_offer = scenario.get("common_offer")
    if not isinstance(common_offer, Mapping):
        return False
    common_segments = _sequence(common_offer.get("segments"))
    if common_segments is None or not common_segments:
        return False
    first_segment = common_segments[0]
    last_segment = common_segments[-1]
    if not isinstance(first_segment, Mapping) or not isinstance(last_segment, Mapping):
        return False
    mode = str(common_offer.get("mode") or signature.get("mode") or "")
    if "avia" in mode.lower() or "air" in mode.lower() or "flight" in mode.lower():
        if len(common_segments) != 1:
            return False
    if not meeting_point_matches(
        contract.hub_code,
        code=str(first_segment.get("origin_code") or ""),
        label=str(first_segment.get("origin_name") or ""),
        mode=mode,
    ) or not meeting_point_matches(
        contract.destination_code,
        code=str(last_segment.get("destination_code") or ""),
        label=str(last_segment.get("destination_name") or ""),
        mode=mode,
    ):
        return False

    feeders = _sequence(scenario.get("feeders"))
    if feeders is None or len(feeders) != len(contract.participants):
        return False
    expected_participants = {participant.id for participant in contract.participants}
    seen_participants: set[str] = set()
    for feeder in feeders:
        if not isinstance(feeder, Mapping):
            return False
        participant_id = str(feeder.get("participant_id") or "")
        if participant_id not in expected_participants or participant_id in seen_participants:
            return False
        seen_participants.add(participant_id)
        try:
            wait = int(feeder["wait_minutes"])
        except (KeyError, TypeError, ValueError):
            return False
        if wait < contract.min_wait_minutes or wait > proposed_max_wait:
            return False
        offer = feeder.get("offer")
        if not isinstance(offer, Mapping):
            return False
        segments = _sequence(offer.get("segments"))
        if not segments or not isinstance(segments[-1], Mapping):
            return False
        if not meeting_point_matches(
            contract.hub_code,
            code=str(segments[-1].get("destination_code") or ""),
            label=str(segments[-1].get("destination_name") or ""),
            mode=mode,
        ):
            return False
    return seen_participants == expected_participants


def _matching_exclusion(scenario: Mapping[str, Any], exclusions: Sequence[JSON]) -> JSON | None:
    """Return an exclusion proved to be recovered by this exact scenario."""

    common_offer = scenario.get("common_offer")
    common_offer_id = str(common_offer.get("id") or "") if isinstance(common_offer, Mapping) else ""
    feeders = _sequence(scenario.get("feeders")) or []
    for exclusion in sorted(exclusions, key=lambda item: (item["participant_id"], item["offer_id"])):
        if common_offer_id != exclusion["common_offer_id"]:
            continue
        for feeder in feeders:
            if not isinstance(feeder, Mapping):
                continue
            participant_id = str(feeder.get("participant_id") or "")
            offer = feeder.get("offer")
            offer_id = str(offer.get("id") or "") if isinstance(offer, Mapping) else ""
            try:
                wait = int(feeder.get("wait_minutes"))
            except (TypeError, ValueError):
                continue
            if (
                participant_id == exclusion["participant_id"]
                and offer_id == exclusion["offer_id"]
                and wait == exclusion["wait_minutes"]
            ):
                return exclusion
    return None


def _public_proposal(
    *,
    scenario: Mapping[str, Any],
    matched_exclusion: Mapping[str, Any],
    original_max_wait: int,
    proposed_max_wait: int,
) -> JSON:
    """Create a browser-safe explanation without carrying any fare data."""

    affected: list[JSON] = []
    for feeder in _sequence(scenario.get("feeders")) or []:
        if not isinstance(feeder, Mapping):
            continue
        try:
            wait = int(feeder.get("wait_minutes"))
        except (TypeError, ValueError):
            continue
        if wait > original_max_wait:
            affected.append(
                {
                    "participant_id": str(feeder.get("participant_id") or ""),
                    "wait_minutes": wait,
                }
            )

    # The matching exclusion is deliberately reduced to a participant and an
    # observed delay.  It gives the UI an auditable reason without exposing an
    # offer identifier, tariff, price, checkout_ref, or handoff URL.
    return {
        "kind": "increase_max_wait",
        "from_max_wait_minutes": original_max_wait,
        "to_max_wait_minutes": proposed_max_wait,
        "delta_minutes": proposed_max_wait - original_max_wait,
        "affected_participants": affected,
        "trigger": {
            "participant_id": str(matched_exclusion["participant_id"]),
            "observed_wait_minutes": int(matched_exclusion["wait_minutes"]),
        },
        "verified": {
            "baseline_scenarios": 0,
            "rerun_scenarios": 1,
            "exact_hub_preserved": True,
            "common_segment_preserved": True,
            "risk_status": STATUS_PASS,
        },
    }


def _sequence(value: Any) -> list[Any] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return None


__all__ = ["suggest_one_max_wait_concession"]

"""Application service joining the agent loop, Tutu MCP, solver and checkout.

The important boundary in this module is the server-side :class:`RunStore`.
Browser requests identify a known selected variant by ``run_id``,
``component_ref`` and ``variant_id``.  They never submit a ``checkout_ref``:
that opaque object stays in memory exactly as it came from Tutu MCP until it is
forwarded to ``create_checkout_link``.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
import inspect
import json
import secrets
import threading
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence

from .conversation import ConversationStore, invoke_optional_kwargs
from .safety import SafetyGate, apply_safety_to_presentation
from .solver import (
    GroupTripContract,
    GroupSyncSolver,
    checkout_ref_for_variant,
    checkout_handoff_guard,
    inspect_offer_risks,
)
from .tutu_mcp import TutuMcpGateway


JSON = dict[str, Any]
AgentRunner = Callable[[str], Any | Awaitable[Any]]


class CheckoutSelectionError(ValueError):
    """Raised for a stale, foreign, or not-yet-selected checkout variant."""


class ConcessionSelectionError(ValueError):
    """Raised when a one-concession replan is stale, forged, or unavailable."""


class ResultBuilder(Protocol):
    """Optional adapter from a runtime result to the UI presentation contract."""

    def __call__(self, value: Any, user_text: str) -> Mapping[str, Any]:
        ...


def _json_copy(value: Any) -> Any:
    """Deep-copy only JSON-compatible data; fail early instead of mutating refs."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("Tutu/run payload must be JSON serializable") from exc


def _mapping(value: Any, *, name: str) -> JSON:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return dict(value)


_REPLAN_COMMON_MODES = frozenset({"avia", "rail", "bus"})
_REPLAN_FEEDER_MODES = frozenset({"avia", "rail", "bus", "etrain", "multitransport"})


def _strict_nonnegative_int(value: Any, *, name: str) -> int:
    """Accept an integer value without silently coercing browser junk."""

    if isinstance(value, bool):
        raise ConcessionSelectionError(f"{name} должен быть целым числом.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ConcessionSelectionError(f"{name} должен быть целым числом.")
    if result < 0:
        raise ConcessionSelectionError(f"{name} не может быть отрицательным.")
    return result


def _canonical_concession_context(
    context: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    requested_max_wait_minutes: Any | None = None,
) -> JSON:
    """Validate and canonicalise the private recipe for one safe replan.

    The browser is deliberately *not* a source of the recipe.  It may repeat
    only the already-rendered proposed upper bound; every route, passenger
    count, luggage rule and lower wait boundary comes from the run-bound
    server context reconstructed below.
    """

    if proposal.get("kind") != "increase_max_wait":
        raise ConcessionSelectionError("Для этого запуска нет подтверждённой уступки.")
    target = _strict_nonnegative_int(
        proposal.get("to_max_wait_minutes"),
        name="Предложенный максимум ожидания",
    )
    original_max = _strict_nonnegative_int(
        proposal.get("from_max_wait_minutes"),
        name="Исходный максимум ожидания",
    )
    if target <= original_max:
        raise ConcessionSelectionError("Предложение уступки содержит некорректное окно ожидания.")
    if requested_max_wait_minutes is not None:
        requested = _strict_nonnegative_int(
            requested_max_wait_minutes,
            name="Максимум ожидания",
        )
        if requested != target:
            raise ConcessionSelectionError("Можно подтвердить только предложенную сервером уступку.")

    context_target = _strict_nonnegative_int(
        context.get("proposed_max_wait_minutes"),
        name="Сохранённый максимум ожидания",
    )
    if context_target != target:
        raise ConcessionSelectionError("Предложение уступки устарело. Выполните поиск заново.")

    raw_contract = context.get("contract")
    if not isinstance(raw_contract, Mapping):
        raise ConcessionSelectionError("Не удалось восстановить исходные условия поездки.")
    try:
        typed = GroupTripContract.from_mapping(raw_contract)
    except ValueError as exc:
        raise ConcessionSelectionError("Не удалось восстановить исходные условия поездки.") from exc
    if typed.max_wait_minutes != original_max:
        raise ConcessionSelectionError("Исходное окно ожидания не совпадает с предложением уступки.")

    raw_participants = raw_contract.get("participants")
    if not isinstance(raw_participants, Sequence) or isinstance(raw_participants, (str, bytes)):
        raise ConcessionSelectionError("Не удалось восстановить состав группы.")
    if len(raw_participants) != len(typed.participants):
        raise ConcessionSelectionError("Не удалось восстановить состав группы.")

    participants: list[JSON] = []
    seen_ids: set[str] = set()
    for raw, typed_participant in zip(raw_participants, typed.participants, strict=True):
        if not isinstance(raw, Mapping):
            raise ConcessionSelectionError("Не удалось восстановить состав группы.")
        # Context is written by PlanningSession in this canonical exact form.
        # Requiring it prevents an alias such as ``name`` from changing the
        # participant identity between the initial and fresh MCP searches.
        if raw.get("id") != typed_participant.id or raw.get("origin_code") != typed_participant.origin_code:
            raise ConcessionSelectionError("Не удалось восстановить состав группы.")
        if typed_participant.id in seen_ids:
            raise ConcessionSelectionError("Не удалось восстановить состав группы.")
        seen_ids.add(typed_participant.id)
        adults = _strict_nonnegative_int(raw.get("adults"), name="Число взрослых")
        if adults < 1:
            raise ConcessionSelectionError("Число взрослых должно быть не меньше одного.")
        participants.append(
            {
                "id": typed_participant.id,
                "origin_code": typed_participant.origin_code,
                "adults": adults,
            }
        )

    raw_date = context.get("departure_date")
    try:
        departure_date = date.fromisoformat(str(raw_date)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ConcessionSelectionError("Не удалось восстановить дату поездки.") from exc
    if str(raw_date) != departure_date:
        raise ConcessionSelectionError("Не удалось восстановить дату поездки.")

    common_mode = str(context.get("common_mode") or "").strip().lower()
    feeder_mode = str(context.get("feeder_mode") or "").strip().lower()
    if common_mode not in _REPLAN_COMMON_MODES or feeder_mode not in _REPLAN_FEEDER_MODES:
        raise ConcessionSelectionError("Не удалось восстановить режимы поездки.")

    contract = typed.to_dict()
    # GroupTripContract deliberately has no passenger-count field.  Keep the
    # already-validated count next to each canonical participant so a new
    # MCP search has exactly the same ticket cardinality as the old one.
    contract["participants"] = participants
    return {
        "contract": contract,
        "departure_date": departure_date,
        "common_mode": common_mode,
        "feeder_mode": feeder_mode,
        "query": str(context.get("query") or "Групповая поездка"),
        "proposed_max_wait_minutes": target,
    }


def _get_offers(value: Any) -> list[JSON]:
    """Accept a raw Tutu response or a direct offers list from a tool caller."""

    if isinstance(value, Mapping):
        for key in ("offers", "results", "items"):
            candidate = value.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                return [dict(item) for item in candidate if isinstance(item, Mapping)]
        nested = value.get("data")
        if isinstance(nested, Mapping):
            return _get_offers(nested)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _passenger_counts(value: Mapping[str, Any] | None) -> JSON:
    value = value or {}
    aliases = {
        "passengers_full": ("passengers_full", "adults", "adult_count", "passengers_adult", "passengers"),
        "passengers_child": ("passengers_child", "children", "child_count"),
        "passengers_infant": ("passengers_infant", "infants", "infant_count"),
    }
    result: JSON = {}
    for canonical, keys in aliases.items():
        for key in keys:
            if key not in value or value[key] is None:
                continue
            if isinstance(value[key], (Mapping, list, tuple)):
                continue
            try:
                result[canonical] = int(value[key])
            except (TypeError, ValueError):
                continue
            break
    return result


def _redact_checkout_refs(value: Any) -> Any:
    """Create the browser-safe shape without exposing opaque checkout refs."""

    if isinstance(value, Mapping):
        return {
            str(key): _redact_checkout_refs(item)
            for key, item in value.items()
            if key not in {"checkout_ref", "checkout_url", "concession_replan_context"}
        }
    if isinstance(value, list):
        return [_redact_checkout_refs(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_checkout_refs(item) for item in value]
    return value


def _variant_id(value: Mapping[str, Any], index: int, *, fallback: str = "default") -> str:
    for key in ("variant_id", "id", "offer_hash", "service_class", "code", "name"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return str(candidate).strip()
    return fallback if index == 1 else f"{fallback}-{index}"


def _canonical_ref(value: Mapping[str, Any]) -> str:
    """Stable key for a JSON opaque ref; never turns it into a new ref."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class CheckoutVariant:
    """A trusted server-side snapshot of one exact tariff choice."""

    variant_id: str
    checkout_ref: JSON
    offer_snapshot: JSON
    expected_passengers: JSON
    public_tariff: JSON = field(default_factory=dict)


@dataclass
class ObservedTutuInventory:
    """Exact offer snapshots returned by MCP tools during one agent run.

    The LLM may quote, sort, or describe these results, but it cannot invent a
    checkout candidate: later components are resolved against this inventory.
    """

    offers_by_ref: dict[str, JSON] = field(default_factory=dict)
    offers_by_fingerprint: dict[str, JSON] = field(default_factory=dict)
    approved_refs: set[str] = field(default_factory=set)

    def observe_tool_result(self, payload: Mapping[str, Any]) -> None:
        self._walk(payload)

    def resolve(self, checkout_ref: Mapping[str, Any], *, require_approved: bool = False) -> JSON | None:
        try:
            key = _canonical_ref(checkout_ref)
        except (TypeError, ValueError):
            return None
        if require_approved and key not in self.approved_refs:
            return None
        snapshot = self.offers_by_ref.get(key)
        return _json_copy(snapshot) if snapshot is not None else None

    def resolve_offer(self, raw_offer: Mapping[str, Any]) -> JSON | None:
        """Return the original MCP offer, not an agent-modified lookalike."""

        try:
            exact = self.offers_by_fingerprint.get(_canonical_ref(raw_offer))
        except (TypeError, ValueError):
            exact = None
        if exact is not None:
            return _json_copy(exact)
        for key in ("checkout_ref",):
            ref = raw_offer.get(key)
            if isinstance(ref, Mapping):
                resolved = self.resolve(ref)
                if resolved is not None:
                    return resolved
        for selected_key in ("selected_variant", "variant", "fare"):
            selected = raw_offer.get(selected_key)
            if isinstance(selected, Mapping) and isinstance(selected.get("checkout_ref"), Mapping):
                resolved = self.resolve(selected["checkout_ref"])
                if resolved is not None:
                    return resolved
        return None

    def approve_solution(self, solution: Mapping[str, Any]) -> None:
        """Permit checkout only for refs emitted by our deterministic solver."""

        scenarios = solution.get("scenarios")
        if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
            return
        for scenario in scenarios:
            if not isinstance(scenario, Mapping):
                continue
            offers: list[Any] = [scenario.get("common_offer")]
            feeders = scenario.get("feeders")
            if isinstance(feeders, Sequence) and not isinstance(feeders, (str, bytes)):
                offers.extend(item.get("offer") for item in feeders if isinstance(item, Mapping))
            for offer in offers:
                if not isinstance(offer, Mapping) or not isinstance(offer.get("checkout_ref"), Mapping):
                    continue
                try:
                    key = _canonical_ref(offer["checkout_ref"])
                except (TypeError, ValueError):
                    continue
                if key in self.offers_by_ref:
                    self.approved_refs.add(key)

    def _walk(self, value: Any) -> None:
        if isinstance(value, Mapping):
            self._record_offer(value)
            for child in value.values():
                self._walk(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                self._walk(child)

    def _record_offer(self, raw: Mapping[str, Any]) -> None:
        # A bare checkout_ref object is not an offer.  Requiring route/fare
        # shape prevents accidental trust of arbitrary nested metadata.
        looks_like_offer = any(key in raw for key in ("legs", "segments", "variants", "fare", "best_offer", "selected_variant"))
        if not looks_like_offer:
            return
        try:
            self.offers_by_fingerprint[_canonical_ref(raw)] = _json_copy(raw)
        except (TypeError, ValueError):
            pass
        variants = raw.get("variants")
        has_variant_list = isinstance(variants, Sequence) and not isinstance(variants, (str, bytes))
        if isinstance(variants, Sequence) and not isinstance(variants, (str, bytes)):
            for variant in variants:
                if not isinstance(variant, Mapping):
                    continue
                exact_ref = checkout_ref_for_variant(raw, variant)
                if exact_ref is None:
                    continue
                snapshot = _json_copy(raw)
                snapshot["selected_variant"] = _json_copy(variant)
                snapshot["checkout_ref"] = _json_copy(exact_ref)
                self._remember(exact_ref, snapshot)
        # Generic top-level refs are safe only on offers without a tariff list
        # (or on a result already narrowed to one selected fare).  Do not turn
        # one generic ref into checkout for every ``variants[]`` entry.
        ref = raw.get("checkout_ref")
        if isinstance(ref, Mapping) and (not has_variant_list or isinstance(raw.get("selected_variant"), Mapping)):
            self._remember(ref, raw)

    def _remember(self, checkout_ref: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        try:
            self.offers_by_ref[_canonical_ref(checkout_ref)] = _json_copy(snapshot)
        except (TypeError, ValueError):
            # An invalid/unsafe JSON object can never be a checkout candidate.
            return


_ACTIVE_TUTU_INVENTORY: ContextVar[ObservedTutuInventory | None] = ContextVar(
    "active_tutu_inventory",
    default=None,
)


@dataclass(frozen=True)
class CheckoutComponent:
    component_ref: str
    variants: Mapping[str, CheckoutVariant]
    public_component: JSON = field(default_factory=dict)


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    presentation: JSON
    components: Mapping[str, CheckoutComponent]
    # Private server-side recipe for a single deterministic fresh search.
    # It is intentionally absent from ``presentation`` and therefore cannot
    # be copied, edited, or replayed by the browser as a travel contract.
    concession_replan_context: JSON | None = None


class RunStore:
    """In-memory per-process inventory of selected fare options.

    A hackathon MVP does not need a database, but this object deliberately has
    a small replacement boundary.  Its invariant is more important than its
    storage choice: external callers cannot inject a checkout ref.
    """

    def __init__(self) -> None:
        self._runs: dict[str, StoredRun] = {}
        # A run is active until a safe concession has claimed it.  The claim
        # closes the small race where two browser requests could both reuse
        # the same displayed card while the fresh MCP search is in flight.
        # We deliberately retain superseded runs for diagnostics, but no
        # checkout or second concession may resolve from them.
        self._lifecycle: dict[str, str] = {}
        # Checkout links may be opened for several components of a single
        # group journey.  A transient lease makes a replan mutually exclusive
        # with an already-started handoff without consuming the whole run.
        self._checkout_leases: dict[str, int] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _new_run_id() -> str:
        return f"run-{secrets.token_urlsafe(12)}"

    def _build_stored(
        self,
        presentation: Mapping[str, Any],
        components: Iterable[CheckoutComponent],
        *,
        concession_replan_context: Mapping[str, Any] | None = None,
        run_id_override: str | None = None,
    ) -> StoredRun:
        """Build an immutable-ish server snapshot before acquiring the lock."""

        run_id = run_id_override or str(presentation.get("run_id") or self._new_run_id())
        public = _json_copy(_redact_checkout_refs(dict(presentation)))
        public["run_id"] = run_id
        component_map = {component.component_ref: component for component in components}
        context: JSON | None = None
        if concession_replan_context is not None:
            proposal = public.get("constraint_negotiator")
            if isinstance(proposal, Mapping):
                # An invalid private recipe fails closed: the baseline search
                # remains usable, but it never gains a replan capability.
                try:
                    context = _canonical_concession_context(concession_replan_context, proposal)
                except ConcessionSelectionError:
                    context = None
        return StoredRun(
            run_id=run_id,
            presentation=public,
            components=component_map,
            concession_replan_context=_json_copy(context) if context is not None else None,
        )

    def _stored_locked(self, run_id: str, *, error_type: type[ValueError]) -> StoredRun:
        stored = self._runs.get(run_id)
        if stored is None:
            raise error_type("Не найден запуск. Выполните поиск заново.")
        return stored

    def _require_active_locked(self, run_id: str, *, error_type: type[ValueError]) -> StoredRun:
        stored = self._stored_locked(run_id, error_type=error_type)
        state = self._lifecycle.get(run_id, "active")
        if state == "active":
            return stored
        if state == "replanning":
            raise error_type("Этот подбор уже пересчитывается. Дождитесь результата или выполните поиск заново.")
        raise error_type("Этот подбор устарел после пересчёта. Используйте новый результат поиска.")

    def _require_concession_active_locked(self, run_id: str) -> StoredRun:
        """Keep the historic public taxonomy: an unknown run is checkout-like.

        An opaque run id is meaningful only in the context of this concession
        endpoint, so both a missing run and a run already being replanned or
        superseded are concession failures.  The checkout path continues to
        raise :class:`CheckoutSelectionError` for its own unknown IDs.
        """

        stored = self._stored_locked(run_id, error_type=ConcessionSelectionError)
        state = self._lifecycle.get(run_id, "active")
        if state == "active":
            return stored
        if state == "replanning":
            raise ConcessionSelectionError(
                "Этот подбор уже пересчитывается. Дождитесь результата или выполните поиск заново."
            )
        raise ConcessionSelectionError("Этот подбор устарел после пересчёта. Используйте новый результат поиска.")

    def put(
        self,
        presentation: Mapping[str, Any],
        components: Iterable[CheckoutComponent],
        *,
        concession_replan_context: Mapping[str, Any] | None = None,
    ) -> StoredRun:
        stored = self._build_stored(
            presentation,
            components,
            concession_replan_context=concession_replan_context,
        )
        with self._lock:
            self._runs[stored.run_id] = stored
            self._lifecycle[stored.run_id] = "active"
            self._checkout_leases[stored.run_id] = 0
        return stored

    def get(self, run_id: str) -> StoredRun:
        with self._lock:
            stored = self._runs.get(run_id)
        if stored is None:
            raise CheckoutSelectionError("Не найден запуск. Выполните поиск заново.")
        return stored

    def resolve_variant(self, run_id: str, component_ref: str, variant_id: str) -> tuple[StoredRun, CheckoutComponent, CheckoutVariant]:
        with self._lock:
            stored = self._require_active_locked(run_id, error_type=CheckoutSelectionError)
            component = stored.components.get(component_ref)
            if component is None:
                raise CheckoutSelectionError("Компонент поездки не относится к этому подбору.")
            variant = component.variants.get(variant_id)
            if variant is None:
                raise CheckoutSelectionError("Тариф не относится к выбранному компоненту поездки.")
            return stored, component, variant

    def claim_variant(self, run_id: str, component_ref: str, variant_id: str) -> tuple[StoredRun, CheckoutComponent, CheckoutVariant]:
        """Resolve one selected fare and hold a short exclusive handoff lease.

        ``create_checkout_link`` performs external MCP I/O after this method
        returns.  Counting that operation lets a concession either start
        before the handoff (and reject it as stale) or reject the concession
        while the handoff is in progress; it cannot interleave invisibly.
        """

        with self._lock:
            stored = self._require_active_locked(run_id, error_type=CheckoutSelectionError)
            component = stored.components.get(component_ref)
            if component is None:
                raise CheckoutSelectionError("Компонент поездки не относится к этому подбору.")
            variant = component.variants.get(variant_id)
            if variant is None:
                raise CheckoutSelectionError("Тариф не относится к выбранному компоненту поездки.")
            self._checkout_leases[run_id] = self._checkout_leases.get(run_id, 0) + 1
            return stored, component, variant

    def release_variant_claim(self, run_id: str) -> None:
        """Release a best-effort handoff lease after the external MCP call."""

        with self._lock:
            current = self._checkout_leases.get(run_id, 0)
            if current > 0:
                self._checkout_leases[run_id] = current - 1

    def resolve_concession_replan(
        self,
        run_id: str,
        proposed_max_wait_minutes: Any,
    ) -> tuple[StoredRun, JSON]:
        """Return the validated server recipe for exactly one displayed offer.

        The supplied maximum is only an equality proof that the user clicked
        the card rendered for this run.  It never patches a contract: all
        other search inputs, including the lower safety bound and passenger
        counts, are rebuilt from private RunStore state.
        """

        with self._lock:
            stored = self._require_concession_active_locked(run_id)
            return stored, self._validated_concession_recipe_locked(
                stored,
                proposed_max_wait_minutes,
            )

    @staticmethod
    def _validated_concession_recipe_locked(stored: StoredRun, proposed_max_wait_minutes: Any) -> JSON:
        proposal = stored.presentation.get("constraint_negotiator")
        context = stored.concession_replan_context
        if not isinstance(proposal, Mapping) or not isinstance(context, Mapping):
            raise ConcessionSelectionError("Для этого запуска нет доступной уступки.")
        return _canonical_concession_context(
            context,
            proposal,
            requested_max_wait_minutes=proposed_max_wait_minutes,
        )

    def begin_concession_replan(
        self,
        run_id: str,
        proposed_max_wait_minutes: Any,
    ) -> tuple[StoredRun, JSON]:
        """Atomically claim one displayed concession before fresh MCP I/O."""

        with self._lock:
            stored = self._require_concession_active_locked(run_id)
            if self._checkout_leases.get(run_id, 0):
                raise ConcessionSelectionError(
                    "По этому подбору уже создаётся ссылка на оформление. Дождитесь результата или выполните новый поиск."
                )
            recipe = self._validated_concession_recipe_locked(stored, proposed_max_wait_minutes)
            self._lifecycle[run_id] = "replanning"
            return stored, recipe

    def abort_concession_replan(self, run_id: str) -> None:
        """Re-open a claimed run only when its fresh replan failed."""

        with self._lock:
            if self._lifecycle.get(run_id) == "replanning":
                self._lifecycle[run_id] = "active"

    def commit_concession_replan(
        self,
        source_run_id: str,
        presentation: Mapping[str, Any],
        components: Iterable[CheckoutComponent],
        *,
        concession_replan_context: Mapping[str, Any] | None = None,
    ) -> StoredRun:
        """Persist a fresh run and supersede its source in one lock section.

        The replacement deliberately always receives a new server-generated
        ID.  A model/result payload must never be able to overwrite the
        source ID and revive its checkout inventory.
        """

        replacement = self._build_stored(
            presentation,
            components,
            concession_replan_context=concession_replan_context,
            run_id_override=self._new_run_id(),
        )
        with self._lock:
            self._stored_locked(source_run_id, error_type=ConcessionSelectionError)
            if self._lifecycle.get(source_run_id) != "replanning":
                raise ConcessionSelectionError("Подтверждение уступки устарело. Выполните поиск заново.")
            # A generated collision is overwhelmingly unlikely, but fail
            # closed instead of replacing an unrelated run if it ever occurs.
            if replacement.run_id in self._runs:
                raise RuntimeError("Не удалось создать новый идентификатор поиска.")
            self._runs[replacement.run_id] = replacement
            self._lifecycle[replacement.run_id] = "active"
            self._checkout_leases[replacement.run_id] = 0
            self._lifecycle[source_run_id] = "superseded"
            self._checkout_leases[source_run_id] = 0
        return replacement


class GroupSyncService:
    """Composition-friendly service with both model tools and UI boundaries.

    ``agent_runner`` is intentionally injected.  It may be an ``AgentRuntime``
    instance, a callable, or a test fake.  This keeps the domain logic free of
    OpenRouter and lets the actual tool-calling loop remain observable.
    """

    def __init__(
        self,
        *,
        tutu: TutuMcpGateway,
        agent_runner: AgentRunner | Any | None = None,
        result_builder: ResultBuilder | None = None,
        solver: GroupSyncSolver | None = None,
        run_store: RunStore | None = None,
        conversations: ConversationStore | None = None,
    ) -> None:
        self.tutu = tutu
        self.agent_runner = agent_runner
        self.result_builder = result_builder
        self.solver = solver or GroupSyncSolver()
        self.run_store = run_store or RunStore()
        self.conversations = conversations or ConversationStore()

    # ------------------------------------------------------------------
    # Text-run / web boundary
    # ------------------------------------------------------------------
    async def run(self, user_text: str, conversation_id: str | None = None) -> JSON:
        """Run the injected agent, persist trusted variants, return UI-safe data."""

        text = str(user_text).strip()
        if not text:
            raise ValueError("user_text must not be empty")
        if self.agent_runner is None:
            raise RuntimeError("GroupSyncService needs an injected agent_runner for text runs")
        cid = str(conversation_id or "").strip()
        prior = self.conversations.prior_messages(cid) if cid else []
        inventory = ObservedTutuInventory()
        inventory_token = _ACTIVE_TUTU_INVENTORY.set(inventory)
        try:
            raw = await self._invoke_runner(text, prior_messages=prior)
        finally:
            _ACTIVE_TUTU_INVENTORY.reset(inventory_token)
        stored = self._store_run_output(raw, user_text=text, inventory=inventory)
        if cid:
            self.conversations.remember(cid, text, stored)
        return stored

    async def replan_concession(self, run_id: str, proposed_max_wait_minutes: Any) -> JSON:
        """Freshly execute exactly one server-bound upper-wait concession.

        Unlike :meth:`run`, this path never asks an LLM to interpret a new
        phrase.  The browser repeats just the displayed number as an equality
        proof; all travel inputs are restored from private RunStore state.
        """

        # Claim first, before the new MCP searches start.  While this request
        # is in progress the old run cannot produce a checkout handoff or a
        # second concurrent replan.  On any failure below it is reopened.
        _, recipe = self.run_store.begin_concession_replan(
            str(run_id),
            proposed_max_wait_minutes,
        )
        try:
            inventory = ObservedTutuInventory()
            inventory_token = _ACTIVE_TUTU_INVENTORY.set(inventory)
            try:
                raw = await self._invoke_concession_replan(recipe)
            finally:
                _ACTIVE_TUTU_INVENTORY.reset(inventory_token)
            return self._store_run_output(
                raw,
                user_text=str(recipe["query"]),
                inventory=inventory,
                supersede_run_id=str(run_id),
            )
        except BaseException:
            # Failed fresh searches must not make the original displayed run
            # disappear.  This applies to MCP/network failures as well as a
            # malformed replan result.
            self.run_store.abort_concession_replan(str(run_id))
            raise

    async def _invoke_runner(self, text: str, *, prior_messages: Sequence[Any] = ()) -> Any:
        runner = self.agent_runner
        target = getattr(runner, "run", None) if runner is not None else None
        callable_target = target if callable(target) else runner
        result = invoke_optional_kwargs(
            callable_target,  # type: ignore[arg-type]
            text,
            prior_messages=prior_messages,
        )
        if inspect.isawaitable(result):
            return await result
        return result

    async def _invoke_concession_replan(self, recipe: Mapping[str, Any]) -> Any:
        runner = self.agent_runner
        target = getattr(runner, "replan_concession", None) if runner is not None else None
        if not callable(target):
            raise ConcessionSelectionError("Этот запуск нельзя безопасно пересчитать. Выполните поиск заново.")
        # Never hand a callback the stored object itself: it must not be able
        # to mutate a later confirmation's recipe in memory.
        result = target(_json_copy(recipe))
        if inspect.isawaitable(result):
            return await result
        return result

    def _store_run_output(
        self,
        raw: Any,
        *,
        user_text: str,
        inventory: ObservedTutuInventory,
        supersede_run_id: str | None = None,
    ) -> JSON:
        presentation = self._build_presentation(raw, user_text)
        presentation = apply_safety_to_presentation(presentation)
        components = self._components_from_payload(presentation, raw_result=raw, inventory=inventory)
        components = self._filter_components_by_safety(presentation, components)
        presentation = self._attach_missing_booking_units(presentation, components)
        if supersede_run_id is None:
            stored = self.run_store.put(
                presentation,
                components,
                concession_replan_context=self._concession_replan_context(raw),
            )
        else:
            stored = self.run_store.commit_concession_replan(
                supersede_run_id,
                presentation,
                components,
                concession_replan_context=self._concession_replan_context(raw),
            )
        return _json_copy(stored.presentation)

    def _require_recommended_checkout(
        self,
        stored: StoredRun,
        component_ref: str,
        *,
        offer: Mapping[str, Any] | None = None,
    ) -> None:
        """Refuse MCP handoff unless a stored scenario is still ``recommended``.

        Verdicts are computed before RunStore redacts ``checkout_ref``, so this
        check uses the persisted ``safety_verdict`` rather than re-parsing a
        stripped public presentation.  A stored single-ticket snapshot is
        re-checked with ``evaluate_offer`` so a short internal connection cannot
        be purchased even if the card was tampered with.
        """

        scenarios = stored.presentation.get("scenarios")
        if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
            if offer is not None:
                SafetyGate().require_recommended(None, None, offer=offer)
            return
        annotated = [item for item in scenarios if isinstance(item, Mapping) and item.get("safety_verdict")]
        if not annotated:
            if offer is not None:
                SafetyGate().require_recommended(None, None, offer=offer)
            return
        scenario_id = component_ref.split(":", 1)[0]
        matched = [item for item in annotated if str(item.get("id")) == scenario_id]
        stored_verdict: str | None = None
        if matched:
            stored_verdict = str(matched[0].get("safety_verdict") or "")
            if stored_verdict != "recommended":
                raise CheckoutSelectionError(
                    "Этот вариант нельзя оформить: он не прошёл проверку безопасности."
                )
            SafetyGate().require_recommended(None, None, stored_verdict=stored_verdict, offer=offer)
            return
        if any(item.get("safety_verdict") == "recommended" for item in annotated):
            SafetyGate().require_recommended(None, None, stored_verdict="recommended", offer=offer)
            return
        raise CheckoutSelectionError(
            "Этот вариант нельзя оформить: он не прошёл проверку безопасности."
        )

    @staticmethod
    def _filter_components_by_safety(
        presentation: Mapping[str, Any],
        components: Sequence[CheckoutComponent],
    ) -> list[CheckoutComponent]:
        scenarios = presentation.get("scenarios")
        if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
            return list(components)
        annotated = [item for item in scenarios if isinstance(item, Mapping) and item.get("safety_verdict")]
        if not annotated:
            return list(components)
        recommended_ids = {
            str(item.get("id"))
            for item in annotated
            if item.get("safety_verdict") == "recommended"
        }
        known_ids = {str(item.get("id")) for item in annotated if item.get("id") is not None}
        kept: list[CheckoutComponent] = []
        for component in components:
            prefix = component.component_ref.split(":", 1)[0]
            if prefix in recommended_ids:
                kept.append(component)
            elif prefix in known_ids:
                continue
            elif recommended_ids:
                kept.append(component)
        return kept

    @staticmethod
    def _concession_replan_context(raw: Any) -> Mapping[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        context = raw.get("concession_replan_context")
        return context if isinstance(context, Mapping) else None

    def _build_presentation(self, raw: Any, user_text: str) -> JSON:
        if self.result_builder is not None:
            return _mapping(self.result_builder(raw, user_text), name="result_builder output")
        if hasattr(raw, "model_dump") and callable(raw.model_dump):
            raw = raw.model_dump(mode="json")
        if not isinstance(raw, Mapping):
            return {"query": user_text, "summary": str(raw), "scenarios": []}
        data = dict(raw)
        nested = data.get("presentation") or data.get("result")
        if isinstance(nested, Mapping):
            presentation = dict(nested)
            for key in ("answer", "summary", "trace", "run_id", "status"):
                if key in data and key not in presentation:
                    presentation[key] = data[key]
        else:
            presentation = data
        presentation.setdefault("query", user_text)
        presentation.setdefault("summary", presentation.get("answer") or "Агент завершил подбор.")
        presentation.setdefault("scenarios", [])
        return _mapping(presentation, name="agent presentation")

    async def create_checkout_link(self, run_id: str, component_ref: str, variant_id: str) -> JSON:
        """Create a Tutu handoff for an explicitly selected stored fare.

        Importantly, there is no ``checkout_ref`` parameter.  A malicious or
        stale browser payload cannot turn this into an arbitrary Tutu URL
        creator because the exact ref is resolved solely from ``RunStore``.
        """

        stored, _, variant = self.run_store.claim_variant(str(run_id), str(component_ref), str(variant_id))
        try:
            self._require_recommended_checkout(
                stored,
                str(component_ref),
                offer=variant.offer_snapshot,
            )
            guard = checkout_handoff_guard(
                variant.offer_snapshot,
                explicit_selection=True,
                selected_checkout_ref=variant.checkout_ref,
                expected_passengers=variant.expected_passengers,
            )
            if not guard["allowed"]:
                raise CheckoutSelectionError("Выбранный тариф больше не проходит проверку: " + ", ".join(guard["errors"]))
            # Requests is synchronous by design in the MCP client; do it outside
            # FastAPI's event loop while preserving the opaque ref byte-for-byte.
            result = await asyncio.to_thread(self.tutu.create_checkout_link, variant.checkout_ref)
            url = str(result.get("checkout_url") or result.get("url") or "").strip()
            if not url:
                raise RuntimeError("Tutu MCP не вернул ссылку на оформление выбранного тарифа")
            raw_kind = str(result.get("kind") or result.get("handoff_kind") or "").strip().lower()
            kind = "search_redirect" if raw_kind == "search_redirect" else "deeplink"
            return {
                "url": url,
                "checkout_url": url,
                "handoff_kind": kind,
                "kind": kind,
                "message": (
                    "Откроется выдача Туту: перед оплатой выберите и проверьте этот вариант."
                    if kind == "search_redirect"
                    else "Откроется оформление на Туту. Перед оплатой проверьте рейс, тариф и пассажиров."
                ),
            }
        finally:
            self.run_store.release_variant_claim(str(run_id))

    # ------------------------------------------------------------------
    # Model-visible, allow-listed travel tools.  ``create_checkout_link`` is
    # deliberately absent: it is an explicit UI action above, not a free LLM
    # side effect during exploratory search.
    # ------------------------------------------------------------------
    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], JSON]]:
        return {
            "get_avia_instructions": lambda _arguments: self.tutu.get_domain_instructions("avia"),
            "get_rail_instructions": lambda _arguments: self.tutu.get_domain_instructions("rail"),
            "get_bus_instructions": lambda _arguments: self.tutu.get_domain_instructions("bus"),
            "get_etrain_instructions": lambda _arguments: self.tutu.get_domain_instructions("etrain"),
            "get_hotels_instructions": lambda _arguments: self.tutu.get_domain_instructions("hotels"),
            "get_multitransport_instructions": lambda _arguments: self.tutu.get_domain_instructions("multitransport"),
            "search_avia": self.search_avia,
            "search_rail": self.search_rail,
            "search_bus": self.search_bus,
            "search_etrain": self.search_etrain,
            "search_hotels": self.search_hotels,
            "search_multitransport": self.search_multitransport,
            "get_offer_details": self.get_offer_details,
            "get_rail_seatmap": self.get_rail_seatmap,
            "solve_group_rendezvous": self.solve_group_rendezvous,
            "inspect_offer_risks": self.inspect_offer_risks,
            "get_search_health": self.get_search_health,
        }

    def search_avia(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.search_avia(arguments))

    def search_rail(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.search_rail(arguments))

    def search_bus(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.search_bus(arguments))

    def search_etrain(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.search_etrain(arguments))

    def search_hotels(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.search_hotels(arguments))

    def search_multitransport(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.search_multitransport(arguments))

    def get_offer_details(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.get_offer_details(arguments))

    def get_rail_seatmap(self, arguments: Mapping[str, Any]) -> JSON:
        return self._observe(self.tutu.get_rail_seatmap(arguments))

    def get_search_health(self, arguments: Mapping[str, Any] | None = None) -> JSON:
        del arguments
        return self.tutu.get_search_health()

    @staticmethod
    def _observe(payload: JSON) -> JSON:
        inventory = _ACTIVE_TUTU_INVENTORY.get()
        if inventory is not None:
            inventory.observe_tool_result(payload)
        return payload

    def solve_group_rendezvous(self, arguments: Mapping[str, Any]) -> JSON:
        contract = arguments.get("contract")
        if not isinstance(contract, Mapping):
            raise ValueError("solve_group_rendezvous requires contract")
        common = _get_offers(arguments.get("common_offers") or arguments.get("common") or [])
        raw_feeders = arguments.get("feeders_by_participant") or arguments.get("feeders")
        if not isinstance(raw_feeders, Mapping):
            raise ValueError("solve_group_rendezvous requires feeders_by_participant")
        feeders = {str(person): _get_offers(value) for person, value in raw_feeders.items()}
        inventory = _ACTIVE_TUTU_INVENTORY.get()
        if inventory is not None:
            common = self._trusted_solver_offers(common, inventory)
            feeders = {
                person: self._trusted_solver_offers(person_offers, inventory)
                for person, person_offers in feeders.items()
            }
        max_scenarios = arguments.get("max_scenarios", 3)
        try:
            max_scenarios = int(max_scenarios)
        except (TypeError, ValueError):
            max_scenarios = 3
        solution = self.solver.solve(contract, common, feeders, max_scenarios=max_scenarios)
        if inventory is not None:
            inventory.approve_solution(solution)
        return solution

    @staticmethod
    def _trusted_solver_offers(raw_offers: Sequence[JSON], inventory: ObservedTutuInventory) -> list[JSON]:
        """Only pass original MCP snapshots into the deterministic solver."""

        trusted: list[JSON] = []
        for raw_offer in raw_offers:
            snapshot = inventory.resolve_offer(raw_offer)
            if snapshot is not None:
                trusted.append(snapshot)
        return trusted

    def inspect_offer_risks(self, arguments: Mapping[str, Any]) -> JSON:
        offer = arguments.get("offer")
        if not isinstance(offer, Mapping):
            raise ValueError("inspect_offer_risks requires a normalized/raw offer")
        required = arguments.get("required_checked_baggage_pieces", 0)
        try:
            required = int(required)
        except (TypeError, ValueError):
            required = 0
        return inspect_offer_risks(offer, required_checked_baggage_pieces=max(0, required))

    # ------------------------------------------------------------------
    # Trusted checkout inventory extraction
    # ------------------------------------------------------------------
    def _components_from_payload(
        self,
        presentation: Mapping[str, Any],
        *,
        raw_result: Any,
        inventory: ObservedTutuInventory,
    ) -> list[CheckoutComponent]:
        explicit = None
        if isinstance(raw_result, Mapping):
            explicit = raw_result.get("checkout_components")
        if explicit is None:
            explicit = presentation.get("checkout_components")
        if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
            return self._explicit_components(explicit, inventory=inventory)
        return self._auto_components(presentation, inventory=inventory)

    def _explicit_components(self, values: Sequence[Any], *, inventory: ObservedTutuInventory) -> list[CheckoutComponent]:
        components: list[CheckoutComponent] = []
        seen: set[str] = set()
        for position, raw_component in enumerate(values, 1):
            component = _mapping(raw_component, name="checkout component")
            component_ref = str(component.get("component_ref") or "").strip()
            if not component_ref or component_ref in seen:
                raise ValueError("checkout component_ref must be unique and non-empty")
            seen.add(component_ref)
            expected = _passenger_counts(component.get("expected_passengers") if isinstance(component.get("expected_passengers"), Mapping) else None)
            variants_value = component.get("variants") or component.get("tariffs")
            if not isinstance(variants_value, Sequence) or isinstance(variants_value, (str, bytes)):
                raise ValueError(f"checkout component {component_ref} needs variants")
            variants: dict[str, CheckoutVariant] = {}
            for index, raw_variant in enumerate(variants_value, 1):
                variant = _mapping(raw_variant, name="checkout variant")
                variant_id = _variant_id(variant, index)
                if variant_id in variants:
                    raise ValueError(f"duplicate variant_id {variant_id!r} in component {component_ref}")
                ref = variant.get("checkout_ref")
                if not isinstance(ref, Mapping):
                    continue
                # The agent-facing result may contain arbitrary JSON.  Trust
                # the opaque ref only when it was observed in a Tutu tool
                # result during this exact run, and use the stored MCP offer
                # rather than the model-supplied ``offer`` snapshot.
                snapshot = inventory.resolve(ref, require_approved=True)
                if snapshot is None:
                    continue
                variant_expected = _passenger_counts(variant.get("expected_passengers") if isinstance(variant.get("expected_passengers"), Mapping) else expected)
                if not variant_expected:
                    variant_expected = _passenger_counts(snapshot.get("checkout_ref") if isinstance(snapshot.get("checkout_ref"), Mapping) else None)
                public_tariff = _redact_checkout_refs({key: value for key, value in variant.items() if key not in {"offer", "expected_passengers"}})
                public_tariff["variant_id"] = variant_id
                variants[variant_id] = CheckoutVariant(
                    variant_id=variant_id,
                    checkout_ref=_json_copy(ref),
                    offer_snapshot=snapshot,
                    expected_passengers=variant_expected,
                    public_tariff=_json_copy(public_tariff),
                )
            if variants:
                public_component = _redact_checkout_refs({key: value for key, value in component.items() if key not in {"variants", "tariffs", "expected_passengers"}})
                components.append(CheckoutComponent(component_ref, variants, _json_copy(public_component)))
        return components

    def _auto_components(self, presentation: Mapping[str, Any], *, inventory: ObservedTutuInventory) -> list[CheckoutComponent]:
        """Build safe inventory from solver-shaped scenarios when possible."""

        components: list[CheckoutComponent] = []
        scenarios = presentation.get("scenarios")
        if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
            return components
        for scenario_index, scenario in enumerate(scenarios, 1):
            if not isinstance(scenario, Mapping):
                continue
            scenario_id = str(scenario.get("id") or scenario_index)
            common = scenario.get("common_offer")
            if isinstance(common, Mapping):
                component = self._component_from_offer(
                    common,
                    component_ref=f"{scenario_id}:common",
                    title="Общее последнее плечо",
                    inventory=inventory,
                )
                if component is not None:
                    components.append(component)
            feeders = scenario.get("feeders")
            if not isinstance(feeders, Sequence) or isinstance(feeders, (str, bytes)):
                continue
            for feeder_index, feeder in enumerate(feeders, 1):
                if not isinstance(feeder, Mapping) or not isinstance(feeder.get("offer"), Mapping):
                    continue
                participant_id = str(feeder.get("participant_id") or feeder_index)
                component = self._component_from_offer(
                    feeder["offer"],
                    component_ref=f"{scenario_id}:feeder:{participant_id}",
                    title=f"Фидер участника {participant_id}",
                    inventory=inventory,
                )
                if component is not None:
                    components.append(component)
        return components

    def _component_from_offer(
        self,
        raw_offer: Mapping[str, Any],
        *,
        component_ref: str,
        title: str,
        inventory: ObservedTutuInventory,
    ) -> CheckoutComponent | None:
        variants: dict[str, CheckoutVariant] = {}
        raw_variants = raw_offer.get("variants")
        if isinstance(raw_variants, Sequence) and not isinstance(raw_variants, (str, bytes)):
            for index, raw_variant in enumerate(raw_variants, 1):
                if not isinstance(raw_variant, Mapping):
                    continue
                exact_ref = checkout_ref_for_variant(raw_offer, raw_variant)
                if exact_ref is None:
                    # There is no generic fallback unless the MCP documented
                    # an exact fare override for this transport product.
                    continue
                variant_id = _variant_id(raw_variant, index)
                if variant_id in variants:
                    continue
                snapshot = inventory.resolve(exact_ref, require_approved=True)
                if snapshot is None:
                    continue
                expected = _passenger_counts(snapshot.get("checkout_ref") if isinstance(snapshot.get("checkout_ref"), Mapping) else None)
                public_tariff = _json_copy(_redact_checkout_refs(raw_variant))
                public_tariff["variant_id"] = variant_id
                variants[variant_id] = CheckoutVariant(
                    variant_id,
                    _json_copy(exact_ref),
                    snapshot,
                    expected,
                    public_tariff,
                )
        top_ref = raw_offer.get("checkout_ref")
        family = str(raw_offer.get("transport") or raw_offer.get("mode") or "").lower()
        avia_families = any(token in family for token in ("avia", "air", "flight"))
        skip_generic_avia = (
            avia_families
            and isinstance(raw_variants, Sequence)
            and not isinstance(raw_variants, (str, bytes))
        )
        if isinstance(top_ref, Mapping) and not variants and not skip_generic_avia:
            default_id = _variant_id(raw_offer, 1, fallback="default")
            if default_id not in variants:
                snapshot = inventory.resolve(top_ref, require_approved=True)
                if snapshot is None:
                    return CheckoutComponent(component_ref, variants, {"component_ref": component_ref, "title": title}) if variants else None
                public_tariff = _json_copy(_redact_checkout_refs(raw_offer))
                public_tariff["variant_id"] = default_id
                variants[default_id] = CheckoutVariant(
                    default_id,
                    _json_copy(top_ref),
                    snapshot,
                    _passenger_counts(snapshot.get("checkout_ref") if isinstance(snapshot.get("checkout_ref"), Mapping) else None),
                    public_tariff,
                )
        if not variants:
            return None
        return CheckoutComponent(component_ref, variants, {"component_ref": component_ref, "title": title})

    @staticmethod
    def _attach_missing_booking_units(presentation: Mapping[str, Any], components: Sequence[CheckoutComponent]) -> JSON:
        """Make solver-shaped results clickable without leaking checkout refs.

        Explicit presentation builders may already supply richer ``booking_units``
        (labels, tariff explanations and scenario placement).  We preserve those.
        For a bare solver response, component references are deterministic enough
        to add the minimal UI cards automatically.
        """

        result = _json_copy(dict(presentation))
        scenarios = result.get("scenarios")
        if not isinstance(scenarios, list):
            return result
        for index, scenario in enumerate(scenarios, 1):
            if not isinstance(scenario, Mapping) or scenario.get("booking_units"):
                continue
            if scenario.get("safety_verdict") and scenario.get("safety_verdict") != "recommended":
                continue
            scenario_id = str(scenario.get("id") or index)
            prefix = f"{scenario_id}:"
            units: list[JSON] = []
            for component in components:
                if not component.component_ref.startswith(prefix):
                    continue
                unit = _json_copy(component.public_component)
                unit["component_ref"] = component.component_ref
                unit["tariffs"] = [
                    _json_copy(variant.public_tariff)
                    for variant in component.variants.values()
                ]
                units.append(unit)
            if units:
                scenario["booking_units"] = units
        return result


__all__ = [
    "CheckoutComponent",
    "CheckoutSelectionError",
    "CheckoutVariant",
    "ConcessionSelectionError",
    "GroupSyncService",
    "RunStore",
    "StoredRun",
]

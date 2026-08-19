"""Smoke tests for the mock-driven GroupSync web interface."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from fastapi.testclient import TestClient

# Works both from repository root and from ``agent/`` as documented in README.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agent.app.web import DemoGroupSyncService, create_app


class RecordingDemoService(DemoGroupSyncService):
    def __init__(self) -> None:
        super().__init__()
        self.searches: list[str] = []
        self.checkout_calls: list[tuple[str, str, str]] = []

    async def run(self, user_text: str, conversation_id: str | None = None):  # type: ignore[no-untyped-def]
        self.searches.append(user_text)
        return await super().run(user_text, conversation_id=conversation_id)

    async def create_checkout_link(  # type: ignore[no-untyped-def]
        self,
        run_id: str,
        component_ref: str,
        variant_id: str,
    ):
        self.checkout_calls.append((run_id, component_ref, variant_id))
        return await super().create_checkout_link(run_id, component_ref, variant_id)


def test_text_search_renders_structured_group_result_and_escapes_input() -> None:
    service = RecordingDemoService()
    client = TestClient(create_app(service))
    query = "Нас трое <script>alert('xss')</script> через IST"

    response = client.post("/search", data={"query": query})

    assert response.status_code == 200
    assert service.searches == [query]
    assert "Как едут:" in response.text
    assert "Пересадка:" in response.text
    assert "VKO 06:10 → IST 10:10" in response.text
    assert "ручная кладь" in response.text
    assert "Варианты:" in response.text
    assert "offer-link" in response.text
    assert "↗" in response.text
    assert "Подтверждено" not in response.text
    assert "Нужно проверить" not in response.text
    assert "Три сценария решения" not in response.text
    assert "Договор поездки" not in response.text
    assert "<script>alert('xss')</script>" not in response.text
    assert "&lt;script&gt;alert" in response.text


def test_checkout_requires_explicit_component_and_exact_tariff() -> None:
    service = RecordingDemoService()
    client = TestClient(create_app(service))
    run = client.post("/api/search", json={"query": "Трое встречаются в IST"}).json()

    missing_selection = client.post("/api/checkout", json={"run_id": run["run_id"]})
    assert missing_selection.status_code == 422
    assert service.checkout_calls == []

    wrong_component = client.post(
        "/api/checkout",
        json={
            "run_id": run["run_id"],
            "component_ref": "moscow-ist",
            "variant_id": "common-flex-v1",
        },
    )
    assert wrong_component.status_code == 422
    assert service.checkout_calls[-1][1:] == ("moscow-ist", "common-flex-v1")

    selected = client.post(
        "/api/checkout",
        json={
            "run_id": run["run_id"],
            "component_ref": "common-ist-lhr",
            "variant_id": "common-flex-v1",
        },
    )
    assert selected.status_code == 200
    assert selected.json()["url"] == "https://www.tutu.ru/avia/"
    assert selected.json()["handoff_kind"] == "search_redirect"
    assert service.checkout_calls[-1][1:] == ("common-ist-lhr", "common-flex-v1")


def test_initial_screen_is_reviewable_without_external_api() -> None:
    client = TestClient(create_app())
    response = client.get("/")

    assert response.status_code == 200
    assert "Демо-режим" not in response.text
    assert "Скажите, как едете" in response.text
    assert "Можно группой" in response.text
    assert 'name="conversation_id"' in response.text
    assert "Диктовка · скоро" not in response.text
    assert ">Диктовка<" in response.text

    # Preview data is stored too: selecting a demo fare has the same explicit
    # handoff flow as a fresh text search.
    handoff = client.post(
        "/api/checkout",
        json={
            "run_id": "demo-preview",
            "component_ref": "common-ist-lhr",
            "variant_id": "common-flex-v1",
        },
    )
    assert handoff.status_code == 200


def test_safe_concession_is_not_shown_as_a_chat_card() -> None:
    class ConcessionDemoService(DemoGroupSyncService):
        async def run(self, user_text: str):  # type: ignore[no-untyped-def]
            result = await super().run(user_text)
            result["scenarios"] = []
            result["constraint_negotiator"] = {
                "from_max_wait_minutes": 300,
                "to_max_wait_minutes": 340,
                "delta_minutes": 40,
                "trigger": {"participant_id": "Аня", "observed_wait_minutes": 340},
                "verified": {"rerun_scenarios": 1},
            }
            return result

    response = TestClient(create_app(ConcessionDemoService())).post(
        "/search", data={"query": "Двое через IST"}
    )

    assert response.status_code == 200
    assert "Цена одной уступки" not in response.text
    assert "Подтвердить и пересчитать" not in response.text
    assert "checkout_ref" not in response.text


def test_concession_post_uses_only_run_and_exact_server_proposed_maximum() -> None:
    class ServerBoundConcessionDemo(DemoGroupSyncService):
        def __init__(self) -> None:
            super().__init__()
            self.replan_calls: list[tuple[str, int]] = []

        async def run(self, user_text: str):  # type: ignore[no-untyped-def]
            result = await super().run(user_text)
            result["scenarios"] = []
            result["constraint_negotiator"] = {
                "kind": "increase_max_wait",
                "from_max_wait_minutes": 300,
                "to_max_wait_minutes": 340,
                "delta_minutes": 40,
                "trigger": {"participant_id": "Аня", "observed_wait_minutes": 340},
                "verified": {"rerun_scenarios": 1},
            }
            # In a live run this private binding lives in RunStore.  The demo
            # has one stored public fixture, so update it for this UI contract.
            self._runs[result["run_id"]] = copy.deepcopy(result)
            return result

        async def replan_concession(self, run_id: str, proposed_max_wait_minutes: int):  # type: ignore[no-untyped-def]
            self.replan_calls.append((run_id, int(proposed_max_wait_minutes)))
            return await super().replan_concession(run_id, proposed_max_wait_minutes)

    service = ServerBoundConcessionDemo()
    client = TestClient(create_app(service))
    initial = client.post("/api/search", json={"query": "Двое через IST"}).json()
    run_id = initial["run_id"]

    # A max different from the card is rejected by the stored run; the extra
    # contract field is deliberately ignored by the web boundary.
    tampered = client.post(
        "/api/concession/replan",
        json={
            "run_id": run_id,
            "proposed_max_wait_minutes": 341,
            "contract": {"hub_code": "SAW", "max_wait_minutes": 1},
        },
    )
    assert tampered.status_code == 422

    foreign = client.post(
        "/api/concession/replan",
        json={"run_id": "foreign-run", "proposed_max_wait_minutes": 340},
    )
    assert foreign.status_code == 422

    fresh = client.post(
        "/api/concession/replan",
        json={"run_id": run_id, "proposed_max_wait_minutes": 340},
    )
    assert fresh.status_code == 200
    assert fresh.json()["run_id"] != run_id
    assert service.replan_calls == [(run_id, 341), ("foreign-run", 340), (run_id, 340)]

    rendered = client.post("/search", data={"query": "Двое через IST"})
    assert 'action="/concession/replan"' not in rendered.text
    assert "Подтверждаю увеличение максимального ожидания" not in rendered.text

    form_run = client.post("/api/search", json={"query": "Ещё двое через IST"}).json()
    form_result = client.post(
        "/concession/replan",
        data={"run_id": form_run["run_id"], "proposed_max_wait_minutes": "340"},
    )
    assert form_result.status_code == 200
    assert "Варианты:" in form_result.text or "Как едут:" in form_result.text


def _conversation_id_from_html(html: str) -> str:
    marker = 'id="conversation-id" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]


def test_conversation_id_is_tab_scoped_and_survives_search_not_refresh() -> None:
    service = RecordingDemoService()
    client = TestClient(create_app(service))
    first = client.get("/")
    second = client.get("/")
    first_id = _conversation_id_from_html(first.text)
    second_id = _conversation_id_from_html(second.text)
    assert first_id.startswith("conv-")
    assert first_id != second_id

    searched = client.post(
        "/search",
        data={"query": "Трое встречаются в IST", "conversation_id": first_id},
    )
    assert searched.status_code == 200
    assert _conversation_id_from_html(searched.text) == first_id
    assert "помнит прошлые уточнения" in searched.text

    follow_up = client.post(
        "/api/search",
        json={"query": "Без багажа", "conversation_id": first_id},
    )
    assert follow_up.status_code == 200
    assert follow_up.json()["conversation_id"] == first_id
    assert service.conversations.turn_count(first_id) == 2
    assert service.conversations.turn_count(second_id) == 0


def test_transcribe_without_stt_backend_is_unavailable() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/transcribe",
        json={"audio_base64": "UklGRg==", "format": "wav"},
    )
    assert response.status_code == 503
    assert "Диктовка" in response.json()["detail"]


def test_healthz_reports_event_loop_liveness() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    with TestClient(create_app()) as lifespan_client:
        probed = lifespan_client.get("/healthz")
    assert probed.status_code == 200
    assert probed.json()["status"] == "ok"
    assert probed.json()["heartbeat_age_seconds"] < 5


def test_readyz_checks_service_contract_without_external_calls() -> None:
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"service_contract": True}}

    class IncompleteService:
        async def run(self, user_text: str):  # type: ignore[no-untyped-def]
            return {"summary": user_text}

    degraded = TestClient(create_app(IncompleteService()))
    response = degraded.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["service_contract"] is False

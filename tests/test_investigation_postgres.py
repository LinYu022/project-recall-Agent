"""Real PostgreSQL with synthetic frozen source records, models and injected faults."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.release_api import create_release_app
from investigation.context import FrozenContext
from investigation.contracts import EvidenceCard, FixedConfig, InvestigationRequest
from investigation.evidence import verify_cards
from investigation.flow import FixedInvestigator
from releases.catalog import Catalog
from releases.contracts import BindTask
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.release_fixtures import release_factory as release_factory
from tests.test_investigation import FakeJSONModel
from tests.test_release_postgres import bind_request, publish, service

pytestmark = pytest.mark.postgres


@pytest.fixture
def investigation_fixture(database, release_factory, tmp_path):
    # Keep content-addressed paths within Windows' default path-length limit.
    tmp_path = tmp_path.parent / ("iv-" + uuid4().hex[:8])
    bundle = release_factory("P1-07")
    catalog = Catalog(database, bundle["store"])
    publish(catalog, bundle)
    catalog.bind(bind_request(bundle, "INV-TEST"))
    runner = service(catalog, tmp_path)
    model = FakeJSONModel()
    investigator = FixedInvestigator(
        runner, model, tmp_path / "runs", FixedConfig(retrieval_method="bm25")
    )
    request = InvestigationRequest(
        task_id="INV-TEST",
        question="SYNTHETIC brake?",
        retrieval_query="brake",
        intent="record_lookup",
    )
    return investigator, request, bundle


def test_fixed_flow_real_catalog_synthetic_model_and_artifacts(investigation_fixture):
    investigator, request, _ = investigation_fixture
    result = investigator.run(request)
    assert result["execution_status"] == "completed", result["error_code"]
    assert result["generation_mode"] == "simulated_model"
    assert result["model_calls"] == 1
    assert result["claim_verification"]["status"] == "passed"
    assert result["semantic_review_status"] == "pending"
    assert len(result["tool_calls"]) <= 6
    store = ArtifactStore(Path(result["artifact_root"]))
    saved = store.read_json(result["report_artifact"]["sha256"])
    assert saved["run_id"] == result["run_id"]
    assert saved["cards"]


@pytest.mark.parametrize(
    "mode,code",
    [
        ("unknown", "ValueError"),
        ("schema", "ValidationError"),
        ("transport", "synthetic_transport_failure"),
        ("truncated", "model_completion_truncated_or_refused"),
    ],
)
def test_model_failure_does_not_become_no_matching_records(investigation_fixture, mode, code):
    investigator, request, _ = investigation_fixture
    investigator.model = FakeJSONModel(mode)
    result = investigator.run(request)
    assert result["execution_status"] == "failed"
    assert result["error_code"] == code
    assert result["answer"] is None
    assert result["report_artifact"]["sha256"]


def test_guards_missing_conditions_time_and_trusted_zero(investigation_fixture):
    investigator, request, bundle = investigation_fixture
    missing = investigator.run(request.model_copy(update={"intent": "applicability"}))
    assert missing["proposal"]["disposition"] == "needs_clarification"
    assert missing["model_calls"] == 0
    from domain.tasks import TimeScope

    historical = investigator.run(
        request.model_copy(
            update={
                "expected_time": TimeScope(
                    mode="historical_as_of",
                    knowledge_cutoff="2014-01-01T00:00:00Z",
                    event_interval=None,
                    date_filter_basis="synthetic historical request",
                )
            }
        )
    )
    assert historical["proposal"]["disposition"] == "unsupported_time"
    assert historical["model_calls"] == 0
    investigator.service.catalog.bind(
        BindTask(
            task_id="INV-ZERO",
            allowed_source_ids=bundle["manifest"]["allowed_source_ids"],
            scope={"make": "TOYOTA", "model": "PRIUS", "model_year": 2025},
        )
    )
    zero = investigator.run(request.model_copy(update={"task_id": "INV-ZERO"}))
    assert zero["proposal"]["disposition"] == "no_matching_records"
    assert zero["cards"][0]["payload"]["counts"]["matched"] == 0
    assert zero["model_calls"] == 0


def test_pending_scope_does_not_become_zero_and_tool_budget_is_enforced(investigation_fixture):
    investigator, request, bundle = investigation_fixture
    investigator.service.catalog.bind(
        BindTask(
            task_id="INV-PENDING",
            allowed_source_ids=bundle["manifest"]["allowed_source_ids"],
            scope={
                "make": "TOYOTA",
                "model": "PRIUS",
                "model_year": 2010,
                "production_date": "2009-01-01",
            },
        )
    )
    result = investigator.run(request.model_copy(update={"task_id": "INV-PENDING"}))
    assert result["cards"][0]["payload"]["status"] == "partial"
    assert not result["proposal"] or result["proposal"]["disposition"] != "no_matching_records"
    investigator.config = FixedConfig(retrieval_method="bm25", max_tool_calls=1)
    limited = investigator.run(request)
    assert limited["error_code"] == "tool_budget_exhausted"
    assert len(limited["tool_calls"]) == 1
    assert limited["model_calls"] == 0


def test_rehashed_origin_tamper_and_cross_task_are_rejected(investigation_fixture):
    from ingestion.records import content_hash

    investigator, request, _ = investigation_fixture
    result = investigator.run(request)
    context = FrozenContext(investigator.service, request.task_id)
    store = ArtifactStore(Path(result["artifact_root"]))
    cards = [EvidenceCard.model_validate(c) for c in result["cards"]]
    changed = cards[0].model_dump(mode="json")
    changed["payload"]["counts"]["matched"] = 100
    origin = store.read_json(changed["origin_sha256"])
    origin["output"] = changed["payload"]
    changed["origin_sha256"] = store.put_json(origin)["sha256"]
    changed["evidence_id"] = content_hash({k: v for k, v in changed.items() if k != "evidence_id"})
    with pytest.raises(ArtifactIntegrityError, match="query replay"):
        verify_cards(context, store, [EvidenceCard.model_validate(changed)])
    wrong = deepcopy(cards[0].model_dump(mode="json"))
    wrong["task_id"] = "OTHER"
    wrong["evidence_id"] = content_hash({k: v for k, v in wrong.items() if k != "evidence_id"})
    with pytest.raises(ArtifactIntegrityError, match="outside task"):
        verify_cards(context, store, [EvidenceCard.model_validate(wrong)])
    focus = next(c for c in cards if c.kind == "text" and c.required_context_ids)
    missing_context = [c for c in cards if c.evidence_id not in focus.required_context_ids]
    with pytest.raises(ArtifactIntegrityError, match="context missing"):
        verify_cards(context, store, missing_context)
    forged = focus.model_dump(mode="json")
    origin = store.read_json(forged["origin_sha256"])
    node = origin["output"]["focus"]["bundle"]["focus"]
    node["citations"][0]["quote"] += " SYNTHETIC FORGERY"
    forged["payload"] = node
    forged["origin_sha256"] = store.put_json(origin)["sha256"]
    forged["evidence_id"] = content_hash({k: v for k, v in forged.items() if k != "evidence_id"})
    with pytest.raises(ArtifactIntegrityError, match="source replay"):
        verify_cards(context, store, [EvidenceCard.model_validate(forged)])
    store.directory.joinpath(cards[0].origin_sha256 + ".json").write_text("{}")
    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        verify_cards(context, store, cards)


def test_investigation_http_is_explicit_and_input_cannot_override_scope(investigation_fixture):
    investigator, request, _ = investigation_fixture
    with TestClient(
        create_release_app(
            development_mode=True, service=investigator.service, investigator=investigator
        )
    ) as client:
        good = client.post("/published/investigate", json=request.model_dump(mode="json"))
        assert good.status_code == 200
        assert good.json()["execution_status"] == "completed"
        assert (
            client.post(
                "/published/investigate", json=request.model_dump(mode="json") | {"scope": {}}
            ).status_code
            == 422
        )
    with TestClient(
        create_release_app(development_mode=True, service=investigator.service)
    ) as client:
        assert (
            client.post("/published/investigate", json=request.model_dump(mode="json")).status_code
            == 404
        )

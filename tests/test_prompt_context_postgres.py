"""Real database and protected HTTP; synthetic sources/scripted models only."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.control_api import create_control_app
from control.contracts import Plan
from investigation.agent import SingleAgent
from investigation.contracts import FixedConfig
from investigation.inspection import inspect_report
from investigation.prompt_context import ContextBinding, restore_summary
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import TOKEN
from tests.test_control_postgres import controlled as controlled
from tests.test_investigation_postgres import investigation_fixture as investigation_fixture
from tests.test_single_agent import ScriptedAgent, decision, finish

pytestmark = pytest.mark.postgres


def test_structured_agent_uses_real_read_tool_and_keeps_history_and_originals(
    investigation_fixture,
):
    fixed, request, bundle = investigation_fixture
    model = ScriptedAgent(
        [
            decision("search", query="brake"),
            decision("expand", document_id="D001"),
            decision("read", evidence_alias="E002", page_size=8),
            finish,
        ]
    )
    result = SingleAgent(
        fixed.service,
        model,
        fixed.root,
        FixedConfig(retrieval_method="bm25", max_model_calls=6, context_mode="structured"),
    ).run(request)
    assert result["execution_status"] == "completed", result["error_code"]
    assert result["proposal"]["disposition"] == "answered"
    assert [c["tool"] for c in result["tool_calls"]] == ["lookup", "search", "expand", "read"]
    assert model.inputs[-1]["observations"][0]["page"]["partial"]
    assert len(result["context_builds"]) == 4 and result["context_artifacts"]
    store = ArtifactStore(Path(result["artifact_root"]))
    binding = ContextBinding.model_validate(result["context_binding"])
    record = result["context_builds"][-1]
    summary = restore_summary(
        store,
        record["summary_artifact"],
        binding,
        expected_snapshot=record["snapshot_artifact"]["sha256"],
    )
    assert summary.model_calls == 0 and summary.provider_usage is None
    page = inspect_report(
        result, store, bundle["directory"], section="evidence", alias="E002", limit=8
    )
    assert page["original_replay"] == "verified_not_semantic_support" and page["partial"]
    citation = next(c for c in result["cards"] if c["kind"] == "text")["payload"]["citations"][0]
    parsed = ArtifactStore(bundle["directory"] / "parsed").directory / (
        citation["parse_artifact_sha256"] + ".json"
    )
    original = parsed.read_bytes()
    try:
        parsed.write_bytes(original + b" ")
        with pytest.raises(ArtifactIntegrityError):
            inspect_report(result, store, bundle["directory"], section="evidence", alias="E002")
    finally:
        parsed.write_bytes(original)


def test_report_pages_are_owned_bounded_replayable_and_revocable(controlled):
    control, submit, _ = controlled
    initial = submit(mode="EXECUTE")
    plan = Plan.model_validate(initial["plan"])
    plan.investigator = "single_agent"
    ready = control.revise("Lin", "LEARN", initial["version"], plan)
    control.model = ScriptedAgent(
        [decision("search", query="brake"), decision("expand", document_id="D001"), finish]
    )
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=6, context_mode="structured"
    )
    completed = control.execute("Lin", "LEARN", ready["version"])
    assert completed["state"] == "COMPLETED"
    with TestClient(
        create_control_app(control, {TOKEN: "Lin", "synthetic-other-token-at-least-24": "Other"})
    ) as client:
        auth = {"Authorization": "Bearer " + TOKEN}
        url = "/tasks/LEARN/context"
        first = client.get(url, params={"section": "observations", "limit": 20}, headers=auth)
        assert first.status_code == 200 and first.json()["next_offset"] == 20
        assert (
            client.get(
                url, headers={"Authorization": "Bearer synthetic-other-token-at-least-24"}
            ).status_code
            == 403
        )
        assert client.get(url).status_code == 403
        assert client.get(url, params={"limit": 4001}, headers=auth).status_code == 422
        assert (
            client.get(
                url, params={"section": "artifact", "artifact_id": "../../.env"}, headers=auth
            ).status_code
            == 422
        )
        evidence = client.get(url, params={"section": "evidence", "alias": "E002"}, headers=auth)
        assert evidence.status_code == 200 and evidence.json()["source_version_ids"]
        control.change_state("Lin", "LEARN", completed["version"], "revoke")
        assert (
            client.get(
                url, params={"section": "evidence", "alias": "E002"}, headers=auth
            ).status_code
            == 403
        )

"""HTTP requests reuse execution guards and expose auditable local artifacts."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app, get_services
from models.adapter import FakeModelAdapter, ModelIdentity
from runtime.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def task_data():
    return json.loads((ROOT / "examples/demo-task.json").read_text(encoding="utf-8"))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_ARTIFACT_DIR", str(tmp_path / "runs"))
    settings = load_settings(ROOT / "examples/dev-readonly.toml")
    application = create_app(settings)
    with TestClient(application) as client:
        yield client, application, settings


def test_success_can_be_traced_to_model_and_tool_artifacts(api, task_data):
    client, _, _ = api
    response = client.post("/demo/runs", json={"task": task_data})
    assert response.status_code == 201
    result = response.json()
    assert "run_dir" not in result
    assert result["model"]["is_simulated"] is True
    assert result["usage"] is None
    assert result["calls_used"] == result["model_calls_used"] == 1
    assert result["call_id"] != result["model_call_id"]
    assert client.get(result["links"]["self"]).json() == result
    events = client.get(result["links"]["events"]).json()["events"]
    assert len(events) == 6
    model_events = [e for e in events if e["event_type"].startswith("model_call_")]
    assert {e["call_id"] for e in model_events} == {result["model_call_id"]}
    assert all(e["run_id"] == result["run_id"] for e in events)
    model_input = client.get(result["links"]["model_input_artifact"]).json()
    assert model_input["request"]["records"][0]["source_id"] == "SIM-COMPLAINT-001"
    model_output = client.get(result["links"]["model_output_artifact"])
    assert model_output.json() == result["summary"]
    assert (
        hashlib.sha256(model_output.content).hexdigest()
        == result["model_output_artifact"]["sha256"]
    )


def test_invalid_body_is_rejected_before_execution_without_echoing_input(api, task_data):
    client, _, settings = api
    del task_data["scope"]["make"]
    task_data["question"] = "PRIVATE_INPUT_SHOULD_NOT_APPEAR"
    response = client.post("/demo/runs", json={"task": task_data})
    assert response.status_code == 422
    assert "PRIVATE_INPUT_SHOULD_NOT_APPEAR" not in response.text
    assert not settings.artifact_dir.exists()


def test_api_cannot_execute_a_tool_outside_the_whitelist(api, task_data):
    client, _, _ = api
    result = client.post("/demo/runs", json={"task": task_data, "tool": "not_allowed"})
    assert result.status_code == 403
    assert result.json()["calls_used"] == result.json()["model_calls_used"] == 0


@pytest.mark.parametrize(
    "body,status,error,model_calls",
    [
        ({"scenario": "empty"}, 201, None, 1),
        ({"scenario": "tool-error"}, 502, "tool_error", 0),
        ({"scenario": "missing-source"}, 502, "tool_output_invalid", 0),
        ({"model_scenario": "model-error"}, 502, "model_error", 1),
        ({"model_scenario": "bad-citation"}, 502, "model_citation_invalid", 1),
        ({"with_model": False}, 201, None, 0),
    ],
)
def test_failure_and_empty_paths_remain_distinct(api, task_data, body, status, error, model_calls):
    client, _, _ = api
    response = client.post("/demo/runs", json={"task": task_data, **body})
    assert response.status_code == status
    result = response.json()
    assert result["error_code"] == error
    assert result["model_calls_used"] == model_calls
    if error:
        assert result["summary"] is None
    if body.get("model_scenario") == "bad-citation":
        assert client.get(result["links"]["model_output_artifact"]).json()["cited_source_ids"] == [
            "UNSEEN-SOURCE"
        ]


def test_model_policy_is_injected_and_checked_before_any_call(api, task_data):
    client, application, _ = api
    services = application.state.services
    services.settings = services.settings.model_copy(update={"max_model_calls": 0})
    application.dependency_overrides[get_services] = lambda: services
    response = client.post("/demo/runs", json={"task": task_data})
    assert response.status_code == 403
    assert response.json()["error_code"] == "model_call_limit_reached"
    assert response.json()["calls_used"] == response.json()["model_calls_used"] == 0


def test_real_provider_is_rejected_before_execution(api, task_data):
    class ForbiddenModel:
        identity = ModelIdentity(
            provider="external", model_id="fake-summary-v1", is_simulated=False
        )

        def generate(self, request):
            pytest.fail("external model must not be called by the demo")

    client, application, _ = api
    application.state.services.model_factory = lambda scenario: ForbiddenModel()
    response = client.post("/demo/runs", json={"task": task_data})
    assert response.status_code == 403
    assert response.json()["error_code"] == "model_not_allowed"
    assert response.json()["calls_used"] == response.json()["model_calls_used"] == 0


def test_malformed_model_response_is_not_a_valid_summary(api, task_data):
    class MalformedModel(FakeModelAdapter):
        def generate(self, request):
            return {"cited_source_ids": ["SIM-COMPLAINT-001"]}

    client, application, _ = api
    application.state.services.model_factory = MalformedModel
    response = client.post("/demo/runs", json={"task": task_data})
    assert response.status_code == 502
    assert response.json()["error_code"] == "model_output_invalid"
    assert response.json()["summary"] is None


def test_artifact_corruption_and_unknown_paths(api, task_data):
    client, _, settings = api
    result = client.post("/demo/runs", json={"task": task_data}).json()
    digest = result["model_output_artifact"]["sha256"]
    artifact = settings.artifact_dir / result["run_id"] / "artifacts" / f"{digest}.json"
    artifact.write_text('{"tampered":true}', encoding="utf-8")
    assert client.get(result["links"]["model_output_artifact"]).status_code == 409
    assert client.get("/demo/runs/" + "0" * 32).status_code == 404
    assert client.get("/demo/runs/not-a-run-id").status_code == 422
    assert client.get(result["links"]["self"] + "/artifacts/" + "0" * 64).status_code == 404


def test_health_and_openapi_are_available(api):
    client, _, _ = api
    assert client.get("/health").json()["model_provider"] == "fake"
    schema = client.get("/openapi.json").json()
    assert "/demo/runs" in schema["paths"]
    assert "task" in schema["components"]["schemas"]["DemoRunRequest"]["required"]

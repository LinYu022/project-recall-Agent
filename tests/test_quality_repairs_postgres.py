"""New development guard integration with real local storage and scripted models only."""

import pytest

from control.contracts import Plan
from control.worker import Worker
from investigation.agent import SingleAgent
from investigation.contracts import FixedConfig
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import controlled as controlled
from tests.test_cooperation_postgres import TeamModel, dispatch, force_two_roles, prepare
from tests.test_investigation_postgres import investigation_fixture as investigation_fixture
from tests.test_single_agent import ScriptedAgent, decision, finish

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("method", ["fixed", "single_agent", "parallel_tools"])
def test_publication_rejects_a_locally_unsupported_trigger_without_extra_calls(
    investigation_fixture, method
):
    fixed, request, _ = investigation_fixture

    def wrong_finish(payload):
        value = finish(payload)
        value["proposal"]["claims"][0]["statement"] = "LFB启用时发生制动问题。"
        return value

    if method == "fixed":
        runner = fixed
        original = runner.model.generate

        def wrong_model(*args, **kwargs):
            response = original(*args, **kwargs)
            response["proposal"]["claims"][0]["statement"] = "LFB启用时发生制动问题。"
            return response

        runner.model.generate = wrong_model
        expected_calls = 1
    else:
        runner = SingleAgent(
            fixed.service,
            ScriptedAgent(
                [
                    decision("search", query="brake"),
                    decision("expand", document_id="D001"),
                    wrong_finish,
                ]
            ),
            fixed.root,
            FixedConfig(retrieval_method="bm25", max_model_calls=3),
            parallel_tools=method == "parallel_tools",
        )
        expected_calls = 3
    result = runner.run(request)
    assert result["execution_status"] == "failed"
    assert result["answer"] is None
    assert result["error_code"] == "answer_publication_checks_failed"
    assert result["output_checks"]["findings"]
    assert result["model_calls"] == expected_calls
    assert result["report_artifact"]


@pytest.mark.parametrize("method", ["fixed", "single_agent"])
def test_no_source_stops_explain_missing_complaints_and_exposure_without_a_model(
    investigation_fixture, method
):
    fixed, request, _ = investigation_fixture
    runner = (
        fixed
        if method == "fixed"
        else SingleAgent(fixed.service, fixed.model, fixed.root, fixed.config)
    )
    result = runner.run(request.model_copy(update={"intent": "complaint_statistics"}))
    assert result["execution_status"] == "completed"
    assert result["proposal"]["disposition"] == "insufficient_evidence"
    assert "投诉全集" in result["answer"] and "暴露量分母" in result["answer"]
    assert result["model_calls"] == 0


def test_control_waiting_prompt_includes_boundary_before_receiving_vehicle_fields(controlled):
    control, submit, _ = controlled
    first = submit()
    plan = Plan.model_validate(first["plan"])
    plan.request.intent = "applicability"
    pending = control.revise("Lin", "LEARN", first["version"], plan)
    assert pending["state"] == "WAITING_INPUT"
    prompt = pending["questions"][0]["prompt"]
    assert "production_date" in prompt and "configurations" in prompt
    assert "完成召回补救" in prompt and "不能据此证明车辆永久安全" in prompt
    assert control.model.calls == 0


def test_coordinator_cannot_publish_the_same_unsupported_trigger(controlled, monkeypatch):
    control, submit, _ = controlled
    model = TeamModel()
    original = model.generate

    def wrong_coordinator(system, payload, *, max_tokens):
        response = original(system, payload, max_tokens=max_tokens)
        if "delegation" not in payload:
            response["proposal"]["claims"][0]["statement"] = "LFB启用时发生制动问题。"
        return response

    model.generate = wrong_coordinator
    ready, module = prepare(control, submit, model=model)
    force_two_roles(monkeypatch, module)
    dispatcher, _ = dispatch(control, ready)
    result = Worker(dispatcher, "quality-repair").run_once()
    assert result["task"]["state"] == "FAILED"
    assert result["task"]["published_report"] is None
    assert len(model.observed["coordinator"]) == 1

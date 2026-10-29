"""Real local PostgreSQL, synthetic sources and scripted model/faults; no external requests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from control.access import AccessDenied
from control.contracts import Plan
from investigation.agent import SingleAgent
from investigation.contracts import FixedConfig
from models.chat_api import ModelCallFailed
from releases.contracts import BindTask
from runtime.artifacts import ArtifactStore
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import controlled as controlled
from tests.test_investigation_postgres import investigation_fixture as investigation_fixture
from tests.test_single_agent import ScriptedAgent, decision, finish

pytestmark = pytest.mark.postgres


def agent(investigation_fixture, steps, **changes):
    fixed, request, _ = investigation_fixture
    model = ScriptedAgent(steps)
    runner = SingleAgent(
        fixed.service,
        model,
        fixed.root,
        FixedConfig(retrieval_method="bm25", max_model_calls=6, **changes),
    )
    return runner, request, model


def test_real_tool_loop_records_plans_evidence_and_verified_atomic_answer(investigation_fixture):
    runner, request, model = agent(
        investigation_fixture,
        [decision("search", query="brake"), decision("expand", document_id="D001"), finish],
    )
    result = runner.run(request)
    assert result["execution_status"] == "completed", result["error_code"]
    assert result["proposal"]["disposition"] == "answered"
    assert result["model_calls"] == 3 and len(result["planner_revisions"]) == 3
    assert result["claim_verification"]["status"] == "passed"
    assert [c["tool"] for c in result["tool_calls"]] == ["lookup", "search", "expand"]
    assert result["semantic_review_status"] == "pending"
    assert "expand" not in model.inputs[0]["available_tools"]
    assert "expand" in model.inputs[1]["available_tools"]
    assert all(
        x["hypothesis_status_is_model_proposal_not_semantic_verdict"]
        for x in result["planner_revisions"]
    )
    assert "reference_claims" not in str(model.inputs)
    assert (
        ArtifactStore(Path(result["artifact_root"])).read_json(result["report_artifact"]["sha256"])[
            "run_id"
        ]
        == result["run_id"]
    )


def test_first_insufficient_search_can_be_rewritten_from_observation(
    investigation_fixture, monkeypatch
):
    runner, request, model = agent(
        investigation_fixture,
        [
            decision("search", query="first controlled empty query"),
            decision("search", query="brake condition"),
            decision("expand", document_id="D001"),
            finish,
        ],
    )
    original = runner.service.query

    def controlled_search(query, **kwargs):
        result = original(query, **kwargs)
        if query.query.startswith("first controlled") and not kwargs:
            return result | {"matched_hits": [], "pending_hits": [], "status": "no_matches"}
        return result

    monkeypatch.setattr(runner.service, "query", controlled_search)
    result = runner.run(request)
    assert result["execution_status"] == "completed"
    assert result["proposal"]["disposition"] == "answered"
    assert model.inputs[1]["observations"][0]["matched"] == 0
    assert [
        c["input"]["request"]["query"] for c in result["tool_calls"] if c["tool"] == "search"
    ] == ["first controlled empty query", "brake condition"]


def test_counterevidence_changes_hypothesis_and_remains_in_final_citations(investigation_fixture):
    def counter_plan(payload):
        card = next(e["alias"] for e in payload["evidence"] if e["kind"] == "text")
        return decision(
            "search",
            query="SYNTHETIC counterexample check",
            hypotheses=[
                {
                    "hypothesis_id": "H001",
                    "statement": "SYNTHETIC universal claim rejected",
                    "status": "contradicted",
                    "support_ids": [],
                    "counter_ids": [card],
                }
            ],
        )

    def finish_counter(payload):
        result = finish(payload, counter=True)
        result["hypotheses"] = payload["previous_decisions"][-1]["hypotheses"]
        return result

    runner, request, _ = agent(
        investigation_fixture,
        [
            decision("search", query="brake"),
            decision("expand", document_id="D001"),
            counter_plan,
            finish_counter,
        ],
    )
    result = runner.run(request)
    assert result["execution_status"] == "completed"
    assert result["recorded_counterevidence_aliases"]
    used = (
        result["proposal"]["claims"][0]["support_ids"]
        + result["proposal"]["claims"][0]["counter_ids"]
    )
    assert set(result["recorded_counterevidence_aliases"]) <= set(used)
    assert result["planner_revisions"][2]["hypotheses"][0]["status"] == "contradicted"


def test_readonly_timeout_retry_is_bounded_and_charged_to_tool_budget(
    investigation_fixture, monkeypatch
):
    runner, request, _ = agent(
        investigation_fixture,
        [decision("search", query="brake"), decision("expand", document_id="D001"), finish],
    )
    original = runner.service.query
    calls = [0]

    def transient(query, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise TimeoutError("SYNTHETIC injected temporary timeout")
        return original(query, **kwargs)

    monkeypatch.setattr(runner.service, "query", transient)
    result = runner.run(request)
    assert result["execution_status"] == "completed"
    assert len(result["tool_calls"]) == 4
    retries = [c for c in result["tool_calls"] if c["tool"] == "search"]
    assert [c["status"] for c in retries] == ["timeout", "completed"]
    assert [c["attempt"] for c in retries] == [1, 2]


@pytest.mark.parametrize(
    "config,reason",
    [
        ({"no_progress_limit": 1}, "no_progress_limit"),
        ({"repeated_action_limit": 1}, "repeated_action_limit"),
        ({"max_tool_calls": 2}, None),
    ],
)
def test_repeated_actions_or_exhausted_tools_stop_with_saved_evidence(
    investigation_fixture, config, reason
):
    runner, request, _ = agent(investigation_fixture, [decision("search", query="brake")], **config)
    result = runner.run(request)
    assert result["cards"] and result["report_artifact"]
    if reason:
        assert (
            result["termination"] == reason
            and result["proposal"]["disposition"] == "insufficient_evidence"
        )
        assert result["quality_status"] == "limited_by_run_budget"
    else:
        assert result["execution_status"] == "failed" and result["answer"] is None


def test_single_call_budget_does_not_prevent_preserving_partial_research(investigation_fixture):
    runner, request, _ = agent(investigation_fixture, [decision("search", query="brake")])
    runner.config = runner.config.model_copy(update={"max_model_calls": 1})
    result = runner.run(request)
    assert result["termination"] == "model_call_budget_exhausted"
    assert (
        result["execution_status"] == "completed"
        and result["proposal"]["disposition"] == "insufficient_evidence"
    )
    assert result["cards"] and len(result["tool_calls"]) == 2


@pytest.mark.parametrize(
    "step",
    [
        decision("expand", document_id="D999"),
        decision("count"),
        decision("search", query="brake") | {"grant_id": "FORGED"},
    ],
)
def test_bad_actions_cannot_escape_server_tool_or_schema_boundary(investigation_fixture, step):
    runner, request, _ = agent(investigation_fixture, [step])
    result = runner.run(request)
    assert result["execution_status"] == "failed" and result["answer"] is None
    assert len(result["tool_calls"]) == 1


def test_protected_control_can_select_agent_but_still_fences_revocation(controlled):
    control, submit, _ = controlled
    first = submit(mode="EXECUTE")
    plan = Plan.model_validate(first["plan"])
    plan.investigator = "single_agent"
    ready = control.revise("Lin", "LEARN", first["version"], plan)
    assert ready["grant_id"] == first["grant_id"]
    entered, release = Event(), Event()
    scripted = ScriptedAgent([decision("search", query="brake")])
    original = scripted.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    scripted.generate = blocked
    control.model = scripted
    control.config = FixedConfig(retrieval_method="bm25", max_model_calls=6)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(control.execute, "Lin", "LEARN", ready["version"])
        assert entered.wait(20)
        running = control.view("Lin", "LEARN")
        control.change_state("Lin", "LEARN", running["version"], "revoke")
        release.set()
        result = future.result(timeout=30)
    assert result["state"] == "PAUSED" and result["published_report"] is None
    with pytest.raises(AccessDenied):
        control.report("Lin", "LEARN")


def test_generation_transport_error_is_not_automatically_retried(investigation_fixture):
    runner, request, model = agent(
        investigation_fixture, [ModelCallFailed("model_transport_failed")]
    )
    result = runner.run(request)
    assert (
        result["execution_status"] == "failed" and result["error_code"] == "model_transport_failed"
    )
    assert result["model_calls"] == model.calls == 1
    assert result["resources"]["provider_usage_totals"]["total_tokens"] is None
    assert not result["resources"]["usage_complete"]


def test_exhausted_timeouts_do_not_become_a_zero_record_answer(investigation_fixture, monkeypatch):
    runner, request, model = agent(
        investigation_fixture, [decision("search", query="brake"), finish]
    )

    def unavailable(*args, **kwargs):
        raise TimeoutError("SYNTHETIC tool remains unavailable")

    monkeypatch.setattr(runner.service, "query", unavailable)
    result = runner.run(request)
    assert [c["status"] for c in result["tool_calls"] if c["tool"] == "search"] == [
        "timeout",
        "timeout",
    ]
    assert (
        result["execution_status"] == "completed"
        and result["proposal"]["disposition"] == "insufficient_evidence"
    )
    assert result["proposal"]["disposition"] != "no_matching_records"
    assert any(o.get("result_is_not_zero") for o in result["observations"])


def test_context_exhaustion_preserves_verified_evidence_without_an_extra_model_call(
    investigation_fixture,
):
    runner, request, model = agent(
        investigation_fixture,
        [decision("search", query="brake")],
        prompt_characters=5000,
        total_prompt_characters=5000,
    )
    runner.plan_snapshot = {"goal": "SYNTHETIC oversized initial plan " + "x" * 6000}
    result = runner.run(request)
    assert result["termination"] == "prompt_budget_exhausted"
    assert result["execution_status"] == "completed" and result["cards"]
    assert model.calls == 0


def test_model_must_not_drop_previously_recorded_counterevidence(investigation_fixture):
    def observed_counter(payload):
        alias = next(e["alias"] for e in payload["evidence"] if e["kind"] == "text")
        return decision(
            "search",
            query="controlled counterexample",
            hypotheses=[
                {
                    "hypothesis_id": "H001",
                    "statement": "SYNTHETIC contradiction",
                    "status": "contradicted",
                    "support_ids": [],
                    "counter_ids": [alias],
                }
            ],
        )

    def omitted_counter(payload):
        result = finish(payload)
        result["proposal"]["claims"][0]["support_ids"] = ["E001"]
        result["proposal"]["claims"][0]["kind"] = "insufficiency"
        return result

    runner, request, _ = agent(
        investigation_fixture,
        [
            decision("search", query="brake"),
            decision("expand", document_id="D001"),
            observed_counter,
            omitted_counter,
        ],
    )
    result = runner.run(request)
    assert result["execution_status"] == "failed" and result["answer"] is None
    assert result["recorded_counterevidence_aliases"]


def test_exhaustive_zero_guard_does_not_depend_on_search_candidates(investigation_fixture):
    fixed, request, bundle = investigation_fixture
    fixed.service.catalog.bind(
        BindTask(
            task_id="AGENT-ZERO",
            release_id=bundle["manifest"]["release_id"],
            allowed_source_ids=bundle["manifest"]["allowed_source_ids"],
            scope={"make": "TOYOTA", "model": "PRIUS", "model_year": 2025},
        )
    )
    scripted = ScriptedAgent([])
    result = SingleAgent(fixed.service, scripted, fixed.root).run(
        request.model_copy(update={"task_id": "AGENT-ZERO"})
    )
    assert result["proposal"]["disposition"] == "no_matching_records" and result["model_calls"] == 0
    assert scripted.calls == 0 and result["evidence_verification"]["status"] == "verified"

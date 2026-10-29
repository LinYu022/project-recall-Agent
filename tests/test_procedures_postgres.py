"""Real SQL/tools/evidence replay over synthetic sources and scripted generation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.control_api import create_control_app
from control.access import AccessDenied, ControlConflict
from control.contracts import Plan, Submission
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.worker import InjectedCrash, Worker
from investigation.contracts import FixedConfig, InvestigationRequest
from memory.contracts import ProjectInput
from memory.episode_contracts import Capture
from memory.procedure_contracts import (
    AbortValidation,
    Candidate,
    Procedure,
    SkillSelection,
    Transition,
    ValidateProcedure,
)
from releases.catalog import Catalog
from runtime.artifacts import ArtifactStore
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import TOKEN, approval_for
from tests.test_dispatch_postgres import expire, make_ready
from tests.test_investigation import FakeJSONModel
from tests.test_release_postgres import publish, service
from tests.test_single_agent import ScriptedAgent, decision

pytestmark = pytest.mark.postgres


@pytest.fixture
def procedures(database, release_factory, tmp_path):
    from control.service import TaskControl

    a = release_factory("P1-15-A", campaign="26V000001")
    b = release_factory("P1-15-B", campaign="26V000002")
    c = release_factory("P1-15-C", campaign="26V000003")
    catalog = Catalog(database, a["store"])
    publish(catalog, a)
    publish(catalog, b, expected=a["manifest"]["release_id"])
    publish(catalog, c, expected=b["manifest"]["release_id"])
    control = TaskControl(
        service(catalog, tmp_path.parent / "pr"),
        FakeJSONModel(),
        tmp_path.parent / "pr-runs",
        config=FixedConfig(retrieval_method="bm25", context_mode="structured", max_model_calls=6),
    )
    control.memory.project(
        "Lin", ProjectInput(project_id="recall", name="SYNTHETIC procedure learning")
    )

    def submit(name, bundle=c, mode="PLAN", *, skill=None, year=2010):
        return control.submit(
            "Lin",
            Submission(
                task_id=name,
                plan=Plan(
                    release_id=bundle["manifest"]["release_id"],
                    allowed_source_ids=bundle["manifest"]["allowed_source_ids"],
                    scope={"make": "TOYOTA", "model": "PRIUS", "model_year": year},
                    request=InvestigationRequest(
                        task_id=name,
                        question="SYNTHETIC recall scope",
                        retrieval_query="brake",
                        intent="record_lookup",
                    ),
                    project_id="recall",
                    mode=mode,
                    skill=skill,
                ),
            ),
        )

    sources = []
    for name, bundle in [("SOURCE-A", a), ("SOURCE-B", b)]:
        ready = submit(name, bundle, mode="EXECUTE")
        assert control.execute("Lin", name, ready["version"])["state"] == "COMPLETED"
        sources.append(
            control.episodes.capture("Lin", Capture(task_id=name, project_id="recall"))[
                "episode_id"
            ]
        )
    definition = Procedure(
        release_ids=[x["manifest"]["release_id"] for x in (a, b, c)],
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    return control, submit, sources, definition, (a, b, c)


def candidate(fixture, *, origins=None, definition=None):
    control, _, source, base, _ = fixture
    return control.procedures.candidate(
        "Lin",
        Candidate(
            project_id="recall",
            skill_key="inspect-scope",
            origin_episode_ids=source if origins is None else origins,
            definition=definition or base,
        ),
    )


def validate_version(control, value, target="TRANSFER", key="check"):
    return control.procedures.validate(
        "Lin",
        value["version_id"],
        ValidateProcedure(
            expected_revision=value["revision"],
            definition_sha256=value["definition_sha256"],
            target_task_ids=[target],
            idempotency_key=key,
        ),
    )


def activate(fixture):
    control, submit, _, _, _ = fixture
    value = candidate(fixture)
    submit("TRANSFER")
    proof = validate_version(control, value)
    assert proof["state"] == "passed", proof
    current = control.procedures.get("Lin", value["version_id"])
    return control.procedures.transition(
        "Lin",
        value["version_id"],
        Transition(
            expected_revision=current["revision"],
            expected_head_revision=current["head"]["revision"],
            action="enable",
            note="SYNTHETIC validated promotion",
        ),
    )


def test_single_success_stays_candidate_and_cannot_enable_or_validate(procedures):
    control, submit, sources, _, _ = procedures
    value = candidate(procedures, origins=sources[:1])
    with pytest.raises(ControlConflict):
        control.procedures.transition(
            "Lin",
            value["version_id"],
            Transition(
                expected_revision=1,
                expected_head_revision=1,
                action="enable",
                note="SYNTHETIC forbidden shortcut",
            ),
        )
    submit("TRANSFER")
    validation = validate_version(control, value)
    assert validation["state"] == "failed" and validation["tool_calls"] == 0
    assert control.procedures.get("Lin", value["version_id"])["state"] == "candidate"


def test_real_transfer_on_nonorigin_event_validates_without_generation(procedures):
    control, submit, _, _, _ = procedures
    value = candidate(procedures)
    submit("TRANSFER")
    before = control.model.calls
    check = validate_version(control, value)
    assert check["state"] == "passed" and check["tool_calls"] == 3
    assert control.model.calls == before
    assert validate_version(control, value)["validation_id"] == check["validation_id"]
    current = control.procedures.get("Lin", value["version_id"])
    assert current["state"] == "validated"
    ref = check["proof"]
    proof = ArtifactStore(Path(ref["artifact_root"])).read_json(ref["artifact"]["sha256"])
    assert proof["transfers"][0]["event_groups"] == ["campaign:26V000003"]


def test_duplicate_origin_event_and_transfer_overlap_cannot_fake_generalization(procedures):
    control, submit, sources, _, bundles = procedures
    old = submit("DUPLICATE", bundles[0], mode="EXECUTE")
    assert control.execute("Lin", "DUPLICATE", old["version"])["state"] == "COMPLETED"
    duplicated = control.episodes.capture("Lin", Capture(task_id="DUPLICATE", project_id="recall"))[
        "episode_id"
    ]
    value = candidate(procedures, origins=[sources[0], duplicated])
    submit("TRANSFER")
    assert validate_version(control, value)["state"] == "failed"
    good = candidate(procedures)
    submit("OVERLAP", bundles[0])
    proof = validate_version(control, good, target="OVERLAP")
    assert proof["state"] == "failed" and proof["tool_calls"] == 1


def test_missing_guard_candidate_fails_before_any_validation_tool(procedures):
    control, submit, _, definition, _ = procedures
    bad = definition.model_copy(update={"required_fields": ["make", "model"]})
    value = candidate(procedures, definition=bad)
    submit("TRANSFER")
    check = validate_version(control, value)
    assert check["state"] == "failed" and check["tool_calls"] == 0
    assert control.procedures.get("Lin", value["version_id"])["state"] == "candidate"


def test_versions_switch_rollback_deprecate_and_irreversible_revoke(procedures):
    control, submit, _, _, _ = procedures
    first = activate(procedures)
    second = candidate(procedures)
    submit("TRANSFER-2")
    assert validate_version(control, second, target="TRANSFER-2")["state"] == "passed"
    current = control.procedures.get("Lin", second["version_id"])
    head = current["head"]["revision"]
    second = control.procedures.transition(
        "Lin",
        second["version_id"],
        Transition(
            expected_revision=2,
            expected_head_revision=head,
            action="enable",
            note="SYNTHETIC new version",
        ),
    )
    old = control.procedures.get("Lin", first["version_id"])
    assert old["state"] == "deprecated"
    restored = control.procedures.rollback(
        "Lin",
        first["version_id"],
        Transition(
            expected_revision=old["revision"],
            expected_head_revision=old["head"]["revision"],
            action="enable",
            note="SYNTHETIC checked rollback",
        ),
    )
    assert restored["state"] == "enabled"
    revoked = control.procedures.transition(
        "Lin",
        first["version_id"],
        Transition(
            expected_revision=restored["revision"],
            expected_head_revision=restored["head"]["revision"],
            action="revoke",
            note="SYNTHETIC retire unsafe version",
        ),
    )
    with pytest.raises(ControlConflict):
        control.procedures.rollback(
            "Lin",
            first["version_id"],
            Transition(
                expected_revision=revoked["revision"],
                expected_head_revision=revoked["head"]["revision"],
                action="enable",
                note="must not resurrect",
            ),
        )


def test_apply_skill_uses_current_parameters_and_charges_each_actual_tool(procedures):
    control, submit, _, _, _ = procedures
    enabled = activate(procedures)
    control.model = ScriptedAgent(
        [
            decision("apply_skill"),
            decision(
                "finish",
                proposal={
                    "disposition": "insufficient_evidence",
                    "claims": [],
                    "unresolved_fields": ["individual_vehicle", "causality"],
                },
            ),
        ]
    )
    initial = submit(
        "USE",
        mode="EXECUTE",
        skill=SkillSelection(project_id="recall", version_id=enabled["version_id"]),
    )
    plan = Plan.model_validate(initial["plan"])
    plan.investigator = "single_agent"
    ready = control.revise("Lin", "USE", initial["version"], plan)
    result = control.execute("Lin", "USE", ready["version"])
    assert result["state"] == "COMPLETED", result
    report = control.report("Lin", "USE")
    assert report["skill_usage"]["mode"] == "applied" and len(report["tool_calls"]) == 3
    assert [t["tool"] for t in report["tool_calls"]] == ["lookup", "search", "expand"]
    assert all(c["task_id"] == report["request"]["task_id"] for c in report["cards"])
    assert len(report["skill_usage"]["applications"]) == 1
    assert report["skill_audit"]["state"] == "active"
    current = control.procedures.get("Lin", enabled["version_id"])
    control.procedures.transition(
        "Lin",
        enabled["version_id"],
        Transition(
            expected_revision=current["revision"],
            expected_head_revision=current["head"]["revision"],
            action="revoke",
            note="SYNTHETIC post-publication audit",
        ),
    )
    assert control.report("Lin", "USE")["skill_audit"]["state"] == "unavailable"


def test_selected_skill_requires_new_plan_authorization_and_cannot_change_project(procedures):
    control, submit, _, _, _ = procedures
    enabled = activate(procedures)
    initial = submit("USE", mode="EXECUTE")
    plan = Plan.model_validate(initial["plan"])
    plan.skill = SkillSelection(project_id="recall", version_id=enabled["version_id"])
    pending = control.revise("Lin", "USE", initial["version"], plan)
    assert pending["state"] == "WAITING_APPROVAL"
    ready = control.approve("Lin", "USE", approval_for(pending))
    assert ready["state"] == "DRAFT"
    with pytest.raises(AccessDenied):
        control.procedures.get("Other", enabled["version_id"])


def test_expired_version_blocks_use(procedures, monkeypatch):
    control, submit, _, definition, _ = procedures
    enabled = activate(procedures)
    selection = SkillSelection(project_id="recall", version_id=enabled["version_id"])
    monkeypatch.setattr(
        "memory.procedures.now", lambda c: definition.expires_at + timedelta(seconds=1)
    )
    initial = submit("USE", mode="EXECUTE", skill=selection)
    result = control.execute("Lin", "USE", initial["version"])
    assert result["state"] == "PAUSED" and result["published_report"] is None


def test_version_revocation_invalidates_enabled_version_and_inflight_publication(procedures):
    control, submit, _, _, _ = procedures
    enabled = activate(procedures)
    selection = SkillSelection(project_id="recall", version_id=enabled["version_id"])
    ready = submit("USE", mode="EXECUTE", skill=selection)
    entered, release = Event(), Event()
    original = control.model.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    control.model.generate = blocked
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "USE", Enqueue(expected_version=ready["version"], idempotency_key="skill-use")
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Worker(d, "skill").run_once, job_id=job["job_id"])
            assert entered.wait(20)
            current = control.procedures.get("Lin", enabled["version_id"])
            control.procedures.transition(
                "Lin",
                enabled["version_id"],
                Transition(
                    expected_revision=current["revision"],
                    expected_head_revision=current["head"]["revision"],
                    action="revoke",
                    note="SYNTHETIC revoke during generation",
                ),
            )
            release.set()
            result = future.result(timeout=30)
        assert result["task"]["state"] == "PAUSED" and result["task"]["published_report"] is None
    finally:
        release.set()


def test_authenticated_lifecycle_api_rejects_owner_forgery(procedures):
    control, _, _, _, _ = procedures
    value = candidate(procedures)
    client = TestClient(
        create_control_app(
            control, {TOKEN: "Lin", "synthetic-other-procedure-owner-token": "Other"}
        )
    )
    assert client.get("/procedures/" + value["version_id"]).status_code == 403
    assert (
        client.get(
            "/procedures/" + value["version_id"],
            headers={"Authorization": "Bearer synthetic-other-procedure-owner-token"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/procedures/candidates",
            headers={"Authorization": "Bearer " + TOKEN},
            json={"owner_id": "Lin"},
        ).status_code
        == 422
    )


def test_skill_application_recovers_saved_steps_with_no_duplicate_search(procedures):
    control, submit, _, _, _ = procedures
    enabled = activate(procedures)
    control.model = ScriptedAgent(
        [
            decision("apply_skill"),
            decision(
                "finish",
                proposal={
                    "disposition": "insufficient_evidence",
                    "claims": [],
                    "unresolved_fields": ["individual_vehicle"],
                },
            ),
        ]
    )
    ready = submit(
        "USE",
        mode="EXECUTE",
        skill=SkillSelection(project_id="recall", version_id=enabled["version_id"]),
    )
    plan = Plan.model_validate(ready["plan"])
    plan.investigator = "single_agent"
    ready = control.revise("Lin", "USE", ready["version"], plan)
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "USE", Enqueue(expected_version=ready["version"], idempotency_key="skill-replay")
    )
    claim = d.claim("old")

    def crash(stage, kind, ordinal):
        if stage == "after_settle" and kind == "tool" and ordinal == 2:
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(d, "old", fault=crash, heartbeat=False).run_claim(claim)
    expire(d, claim)
    d.recover_expired()
    make_ready(d, job["job_id"])
    result = Worker(d, "new", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED", result
    assert result["calls"]["replayed_tool_calls"] == 2 and result["calls"]["new_tool_calls"] == 1


def test_aborted_inflight_validation_cannot_promote_late_result(procedures, monkeypatch):
    from memory import procedure_schema as db

    control, submit, _, _, _ = procedures
    value = candidate(procedures)
    submit("TRANSFER")
    entered, release = Event(), Event()
    original = control.service.query

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(control.service, "query", blocked)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(validate_version, control, value)
            assert entered.wait(20)
            with control.catalog.transaction() as connection:
                identifier = connection.scalar(sa.select(db.validations.c.validation_id))
            stopped = control.procedures.abort_validation(
                "Lin", identifier, AbortValidation(note="SYNTHETIC concurrent abort")
            )
            assert stopped["state"] == "failed"
            release.set()
            final = future.result(timeout=30)
        assert final["state"] == "failed" and final["tool_calls"] == 2
        assert control.procedures.get("Lin", value["version_id"])["state"] == "candidate"
    finally:
        release.set()


def test_unknown_interruption_stays_running_without_automatic_retry(procedures, monkeypatch):
    control, submit, _, _, _ = procedures
    value = candidate(procedures)
    submit("TRANSFER")

    def interrupted(*args, **kwargs):
        raise RuntimeError("SYNTHETIC unexpected process interruption")

    monkeypatch.setattr("memory.procedures.execute_scope_recipe", interrupted)
    with pytest.raises(RuntimeError):
        validate_version(control, value)
    pending = validate_version(control, value)
    assert pending["state"] == "running" and pending["tool_calls"] == 0
    client = TestClient(create_control_app(control, {TOKEN: "Lin"}))
    path = "/procedure-validations/" + pending["validation_id"]
    assert client.get(path).status_code == 403
    headers = {"Authorization": "Bearer " + TOKEN}
    assert client.get(path, headers=headers).json()["state"] == "running"
    assert (
        client.post(path + "/abort", headers=headers, json={"note": "SYNTHETIC close"}).status_code
        == 200
    )
    assert validate_version(control, value)["state"] == "failed"


def test_multiple_targets_cannot_count_same_event_twice(procedures):
    control, submit, _, _, _ = procedures
    value = candidate(procedures)
    submit("TARGET-ONE")
    submit("TARGET-TWO")
    check = control.procedures.validate(
        "Lin",
        value["version_id"],
        ValidateProcedure(
            expected_revision=1,
            definition_sha256=value["definition_sha256"],
            target_task_ids=["TARGET-ONE", "TARGET-TWO"],
            idempotency_key="same-event",
        ),
    )
    assert check["state"] == "failed" and check["tool_calls"] == 4
    assert control.procedures.get("Lin", value["version_id"])["state"] == "candidate"


@pytest.mark.parametrize("change", ["origin_disabled", "target_revoked", "proof_missing"])
def test_provenance_changes_block_enabled_use(procedures, change):
    from memory.episode_contracts import HistoryChange

    control, submit, sources, _, _ = procedures
    enabled = activate(procedures)
    if change == "origin_disabled":
        control.episodes.change(
            "Lin", sources[0], HistoryChange(expected_revision=1, enabled=False)
        )
    elif change == "target_revoked":
        target = control.view("Lin", "TRANSFER")
        control.change_state("Lin", "TRANSFER", target["version"], "revoke")
    else:
        validation = control.procedures.validation("Lin", enabled["validation_id"])
        pointer = validation["proof"]
        store = ArtifactStore(Path(pointer["artifact_root"]))
        (store.directory / (pointer["artifact"]["sha256"] + ".json")).unlink()
    ready = submit(
        "USE",
        mode="EXECUTE",
        skill=SkillSelection(project_id="recall", version_id=enabled["version_id"]),
    )
    if change == "proof_missing":
        with pytest.raises((ValueError, OSError)):
            control.execute("Lin", "USE", ready["version"])
        assert control.view("Lin", "USE")["published_report"] is None
    else:
        result = control.execute("Lin", "USE", ready["version"])
        assert result["state"] == "PAUSED" and result["published_report"] is None


def test_selected_skill_rejects_unknown_year_even_for_campaign_bound_task(procedures):
    control, submit, _, _, _ = procedures
    enabled = activate(procedures)
    ready = submit("TEMPLATE", mode="EXECUTE")
    plan = Plan.model_validate(ready["plan"])
    plan.scope = plan.scope.model_copy(update={"model_year": None, "campaign_number": "26V000003"})
    plan.request = plan.request.model_copy(update={"task_id": "MISSING-YEAR"})
    plan.skill = SkillSelection(project_id="recall", version_id=enabled["version_id"])
    ready = control.submit("Lin", Submission(task_id="MISSING-YEAR", plan=plan))
    assert ready["state"] == "DRAFT"
    result = control.execute("Lin", "MISSING-YEAR", ready["version"])
    assert result["state"] == "PAUSED" and result["published_report"] is None


def test_validation_cannot_borrow_existing_dispatcher_root_budget(procedures):
    control, submit, _, _, _ = procedures
    value = candidate(procedures)
    ready = submit("DISPATCHED", mode="EXECUTE")
    dispatcher = Dispatcher(control)
    dispatcher.enqueue(
        "Lin", "DISPATCHED", Enqueue(expected_version=ready["version"], idempotency_key="pool")
    )
    current = control.view("Lin", "DISPATCHED")
    research = Plan.model_validate(current["plan"])
    research.mode = "PLAN"
    control.revise("Lin", "DISPATCHED", current["version"], research)
    check = validate_version(control, value, target="DISPATCHED")
    assert check["state"] == "failed" and check["tool_calls"] == 0
    pointer = check["proof"]
    proof = ArtifactStore(Path(pointer["artifact_root"])).read_json(pointer["artifact"]["sha256"])
    assert proof["failure_code"] == "procedure_validation_cannot_bypass_dispatched_budget"

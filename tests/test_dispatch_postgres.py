"""Real PostgreSQL and threads; all sources/models synthetic and no provider network."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.control_api import create_control_app
from control.access import AccessDenied, ControlConflict, using, validate
from control.contracts import Plan, Research, Submission
from control.dispatch import Dispatcher, db_now
from control.journal import Journal
from control.queue_contracts import Enqueue, Reconcile, vector
from control.queue_schema import calls, jobs
from control.schema import executions
from control.service import TaskControl
from control.worker import InjectedCrash, Worker
from investigation.contracts import FixedConfig
from models.chat_api import ModelCallFailed
from releases.contracts import ReleaseQuery
from runtime.artifacts import ArtifactIntegrityError
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import TOKEN
from tests.test_control_postgres import controlled as controlled

pytestmark = pytest.mark.postgres


def enqueue(control, ready, **changes):
    original = control.model.generate

    def metered(*args, **kwargs):
        return original(*args, **kwargs) | {
            "usage": {"prompt_tokens": 30, "completion_tokens": 80, "total_tokens": 110},
            "usage_is_synthetic": True,
        }

    control.model.generate = metered
    dispatch = Dispatcher(control)
    value = Enqueue(expected_version=ready["version"], idempotency_key="first", **changes)
    return dispatch, dispatch.enqueue("Lin", ready["task_id"], value)


def expire(dispatch, claim):
    with dispatch.catalog.engine.begin() as connection:
        connection.execute(
            executions.update()
            .where(executions.c.execution_id == claim.access.execution_id)
            .values(lease_until=db_now(connection) - timedelta(seconds=1))
        )


def make_ready(dispatch, job_id):
    with dispatch.catalog.engine.begin() as connection:
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job_id)
            .values(available_at=db_now(connection) - timedelta(seconds=1))
        )


def test_duplicate_enqueue_concurrent_claim_and_heartbeat_have_one_owner(controlled):
    control, submit, _ = controlled
    ready = submit(mode="EXECUTE")
    dispatch, job = enqueue(control, ready)
    duplicate = dispatch.enqueue(
        "Lin", "LEARN", Enqueue(expected_version=ready["version"], idempotency_key="first")
    )
    assert duplicate["job_id"] == job["job_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda w: dispatch.claim(w), ["worker-a", "worker-b"]))
    claim = next(c for c in claims if c is not None)
    assert sum(c is not None for c in claims) == 1
    dispatch.heartbeat(claim)
    result = Worker(dispatch, "winner", heartbeat=False).run_claim(claim)
    assert result["task"]["state"] == "COMPLETED" and result["calls"]["new_model_calls"] == 1
    assert dispatch.view("Lin", "LEARN")["jobs"][0]["state"] == "completed"
    assert dispatch.claim("duplicate") is None
    with pytest.raises(AccessDenied):
        dispatch.heartbeat(claim)


@pytest.mark.parametrize("kind", ["tool", "model"])
def test_saved_step_replays_after_worker_exit_and_service_reconstruction(controlled, kind):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    claim = dispatch.claim("dead")

    def fault(stage, current, ordinal):
        if stage == "after_settle" and current == kind:
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(dispatch, "dead", fault=fault, heartbeat=False).run_claim(claim)
    before = control.model.calls
    expire(dispatch, claim)
    rebuilt = TaskControl(control.service, control.model, control.root, config=control.config)
    recovered = Dispatcher(rebuilt)
    assert recovered.recover_expired() == [job["job_id"]]
    make_ready(recovered, job["job_id"])
    result = Worker(recovered, "new", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED", result
    assert result["calls"]["replayed_tool_calls"] >= 1
    if kind == "model":
        assert result["calls"]["new_model_calls"] == 0 and control.model.calls == before
    assert recovered.view("Lin", "LEARN")["resources"]["used"]["model_calls"] == 1
    with using(claim.access):
        with pytest.raises(AccessDenied):
            control.service.query(
                ReleaseQuery(task_id=claim.revision["binding_task_id"], query="brake")
            )


def test_unknown_inflight_model_is_held_for_review_and_never_automatically_repeated(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    claim = dispatch.claim("dead")

    def fault(stage, kind, ordinal):
        if stage == "after_response_before_save" and kind == "model":
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(dispatch, "dead", fault=fault, heartbeat=False).run_claim(claim)
    expire(dispatch, claim)
    dispatch.recover_expired()
    status = dispatch.view("Lin", "LEARN")
    assert status["jobs"][0]["state"] == "waiting_input"
    pending = next(c for c in status["calls"] if c["kind"] == "model")
    assert pending["pending_reconciliation"] and pending["charged"]["completion_tokens"] == 2000
    assert Worker(dispatch, "other").run_once() is None and control.model.calls == 1
    paused = control.view("Lin", "LEARN")
    resumed = control.change_state("Lin", "LEARN", paused["version"], "resume")
    with pytest.raises(ControlConflict):
        dispatch.recover("Lin", "LEARN", resumed["version"])
    dispatch.reconcile(
        "Lin",
        "LEARN",
        Reconcile(
            call_id=pending["call_id"],
            completion_tokens=80,
            note="SYNTHETIC billing record checked",
        ),
    )
    closed = dispatch.recover("Lin", "LEARN", resumed["version"])
    assert (
        closed["jobs"][0]["state"] == "failed" and control.view("Lin", "LEARN")["state"] == "FAILED"
    )
    assert control.model.calls == 1


def test_shared_parent_pool_last_allowance_has_only_one_concurrent_reservation(controlled):
    control, submit, _ = controlled
    parent = submit(name="PARENT", mode="EXECUTE")
    children = []
    for name in ("LEFT", "RIGHT"):
        p = Plan.model_validate(parent["plan"])
        p.request = p.request.model_copy(update={"task_id": name})
        children.append(control.submit("Lin", Submission(task_id=name, plan=p, parent_id="PARENT")))
    dispatch = Dispatcher(control)
    for c in children:
        dispatch.enqueue(
            "Lin", c["task_id"], Enqueue(expected_version=c["version"], idempotency_key="first")
        )
    claims = [dispatch.claim("w1"), dispatch.claim("w2")]

    def reserve(claim):
        try:
            return dispatch.reserve(
                claim,
                0,
                "model",
                "a" * 64,
                vector(model_calls=1, completion_tokens=2000),
                dispatch.store(claim.job_id).put_json({"synthetic": True}),
            )
        except ModelCallFailed:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, claims))
    assert sum(r is not None for r in results) == 1
    status = dispatch.view("Lin", "LEFT")
    assert (
        status["resources"]["root_task_id"] == "PARENT"
        and status["resources"]["held"]["model_calls"] == 1
    )
    # A fresh service cannot route around the existing root budget using the legacy method.
    legacy = TaskControl(control.service, control.model, control.root, config=control.config)
    with pytest.raises(AccessDenied):
        legacy.execute("Lin", "PARENT", parent["version"])


def test_cancellation_during_model_keeps_late_response_private_and_settles_actual_usage(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    entered, release = Event(), Event()
    original = control.model.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    control.model.generate = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Worker(dispatch, "blocked", heartbeat=False).run_once)
        assert entered.wait(20)
        running = control.view("Lin", "LEARN")
        control.change_state("Lin", "LEARN", running["version"], "cancel")
        dispatch.recover_expired()
        release.set()
        result = future.result(timeout=30)
    assert result["task"]["state"] == "CANCELLED" and result["task"]["published_report"] is None
    status = dispatch.view("Lin", "LEARN")
    late = next(c for c in status["calls"] if c["kind"] == "model")
    assert late["late"] and late["state"] == "completed"
    assert status["resources"]["held"] == vector()
    assert status["resources"]["used"]["completion_tokens"] == 80


@pytest.mark.parametrize("change", ["runtime", "grant", "release_file", "call_artifact"])
def test_recovery_refuses_changed_runtime_authorization_or_missing_pinned_data(controlled, change):
    control, submit, bundle = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    if change == "runtime":
        control.model.identity = control.model.identity | {"changed": True}
    elif change == "grant":
        current = control.view("Lin", "LEARN")
        control.change_state("Lin", "LEARN", current["version"], "revoke")
    elif change == "release_file":
        manifest_path = bundle["store"].manifests.directory / (
            bundle["sealed"]["manifest_sha256"] + ".json"
        )
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    else:
        claim = dispatch.claim("first")
        journal = Journal(dispatch, claim, control.model)
        journal.tool("synthetic", {}, lambda: {"ok": True})
        with dispatch.catalog.engine.begin() as connection:
            ref = connection.scalar(
                sa.select(calls.c.output_artifact).where(calls.c.job_id == job["job_id"])
            )
        (dispatch.store(job["job_id"]).directory / (ref["sha256"] + ".json")).unlink()
        expire(dispatch, claim)
        dispatch.recover_expired()
        make_ready(dispatch, job["job_id"])
    assert dispatch.claim("new") is None and control.model.calls == 0


def test_deadline_attempt_limit_and_authenticated_dispatch_api(controlled):
    control, submit, _ = controlled
    ready = submit(mode="EXECUTE")
    dispatch = Dispatcher(control)
    with TestClient(
        create_control_app(control, {TOKEN: "Lin", "synthetic-other-token-at-least-24": "Other"})
    ) as client:
        url = "/tasks/LEARN/dispatch"
        auth = {"Authorization": "Bearer " + TOKEN}
        body = {"expected_version": ready["version"], "idempotency_key": "api", "max_attempts": 1}
        assert client.post(url, json=body).status_code == 403
        response = client.post(url, json=body, headers=auth)
        assert response.status_code == 200 and response.json()["state"] == "queued"
        assert (
            client.post(url, json=body, headers=auth).json()["job_id"] == response.json()["job_id"]
        )
        assert (
            client.get(
                url, headers={"Authorization": "Bearer synthetic-other-token-at-least-24"}
            ).status_code
            == 403
        )
        claim = dispatch.claim("dead")
        expire(dispatch, claim)
        dispatch.recover_expired()
        assert dispatch.view("Lin", "LEARN")["jobs"][0]["state"] == "failed"
        assert control.view("Lin", "LEARN")["state"] == "FAILED"


def test_process_exit_after_publication_does_not_create_a_second_official_result(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))

    def fault(stage, kind, ordinal):
        if stage == "after_publication":
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(dispatch, "dead", fault=fault, heartbeat=False).run_once()
    assert control.view("Lin", "LEARN")["state"] == "COMPLETED"
    dispatch.recover_expired()
    assert dispatch.view("Lin", "LEARN")["jobs"][0]["state"] == "completed"
    assert Worker(dispatch, "again").run_once() is None and control.model.calls == 1


def test_deadline_blocks_old_permit_even_without_a_reaper(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    claim = dispatch.claim("deadline")
    with dispatch.catalog.engine.begin() as connection:
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job["job_id"])
            .values(deadline=db_now(connection) - timedelta(seconds=1))
        )
    with pytest.raises(AccessDenied):
        dispatch.heartbeat(claim)
    with dispatch.catalog.engine.begin() as connection:
        with pytest.raises(AccessDenied):
            validate(connection, claim.access, "publish", db_now(connection) - timedelta(days=1))
    dispatch.recover_expired()
    assert dispatch.view("Lin", "LEARN")["jobs"][0]["state"] == "failed"
    assert control.model.calls == 0


def test_old_worker_cannot_publish_after_lease_expiry_and_new_claim(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    old = dispatch.claim("old")
    expire(dispatch, old)
    dispatch.recover_expired()
    make_ready(dispatch, job["job_id"])
    current = dispatch.claim("new")
    assert current.access.epoch > old.access.epoch
    assert (
        Worker(dispatch, "old", heartbeat=False).run_claim(old)["task"]["published_report"] is None
    )
    assert (
        Worker(dispatch, "new", heartbeat=False).run_claim(current)["task"]["state"] == "COMPLETED"
    )
    assert control.model.calls == 1


def test_readonly_unknown_step_retries_with_an_additional_shared_charge(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    old = dispatch.claim("old")

    def crash(stage, kind, ordinal):
        if stage == "after_response_before_save" and kind == "tool":
            raise InjectedCrash()

    journal = Journal(dispatch, old, control.model, fault=crash)
    with pytest.raises(InjectedCrash):
        journal.tool("readonly", {}, lambda: {"value": 1})
    expire(dispatch, old)
    dispatch.recover_expired()
    make_ready(dispatch, job["job_id"])
    claim = dispatch.claim("new")
    fresh = Journal(dispatch, claim, control.model)
    assert fresh.tool("readonly", {}, lambda: {"value": 1}) == {"value": 1}
    assert dispatch.view("Lin", "LEARN")["resources"]["used"]["tool_calls"] == 2


def test_replay_refuses_a_changed_step_instead_of_silently_issuing_a_new_request(controlled):
    control, submit, _ = controlled
    dispatch, job = enqueue(control, submit(mode="EXECUTE"))
    claim = dispatch.claim("first")
    Journal(dispatch, claim, control.model).tool("readonly", {"query": "old"}, lambda: {"value": 1})
    expire(dispatch, claim)
    dispatch.recover_expired()
    make_ready(dispatch, job["job_id"])
    current = dispatch.claim("new")
    with pytest.raises(ArtifactIntegrityError, match="input_changed"):
        Journal(dispatch, current, control.model).tool(
            "readonly", {"query": "changed"}, lambda: {"value": 2}
        )


def test_structured_agent_recovers_saved_decision_with_new_epoch_and_rebuilt_context(controlled):
    from scripts.verify_single_agent_real import SyntheticDecisionModel

    control, submit, _ = controlled
    ready = submit(mode="EXECUTE")
    plan = Plan.model_validate(ready["plan"])
    plan.investigator = "single_agent"
    ready = control.revise("Lin", "LEARN", ready["version"], plan)
    control.model = SyntheticDecisionModel()
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=6, context_mode="structured"
    )
    dispatch, job = enqueue(control, ready)
    claim = dispatch.claim("old")

    def fault(stage, kind, ordinal):
        if stage == "after_settle" and kind == "model":
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(dispatch, "old", fault=fault, heartbeat=False).run_claim(claim)
    expire(dispatch, claim)
    dispatch.recover_expired()
    make_ready(dispatch, job["job_id"])
    result = Worker(dispatch, "new", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED", result
    assert result["calls"]["replayed_model_calls"] == 1
    report = control.report("Lin", "LEARN")
    assert report["context_binding"]["epoch"] > claim.access.epoch and report["context_builds"]


def test_idempotency_payload_conflicts_and_manual_research_cannot_bypass_root_pool(controlled):
    control, submit, _ = controlled
    ready = submit(mode="EXECUTE")
    dispatch, job = enqueue(control, ready)
    with pytest.raises(ControlConflict):
        dispatch.enqueue(
            "Lin",
            "LEARN",
            Enqueue(expected_version=ready["version"], idempotency_key="first", max_attempts=5),
        )
    with pytest.raises(AccessDenied):
        control.research("Lin", "LEARN", Research(operation="search", query="brake"))
    assert control.model.calls == 0


def test_queued_job_retains_its_release_before_first_worker_has_created_a_binding(controlled):
    control, submit, bundle = controlled
    ready = submit(mode="EXECUTE")
    dispatch, _ = enqueue(control, ready)
    release = bundle["manifest"]["release_id"]
    assert "active_dispatch_jobs" in control.catalog.collect(release)["reasons"]
    queued = control.view("Lin", "LEARN")
    control.change_state("Lin", "LEARN", queued["version"], "cancel")
    assert dispatch.view("Lin", "LEARN")["jobs"][0]["state"] == "cancelled"
    assert "active_dispatch_jobs" not in control.catalog.collect(release)["reasons"]

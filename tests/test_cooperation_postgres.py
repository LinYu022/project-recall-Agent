"""Real PostgreSQL and independent threads; frozen fixtures and model responses are synthetic."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa

from control.contracts import Plan
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.queue_schema import calls
from control.worker import Worker
from investigation.contracts import FixedConfig
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import controlled as controlled
from tests.test_single_agent import ScriptedAgent, decision, finish

pytestmark = pytest.mark.postgres


class TeamModel:
    identity = {"model_id": "synthetic-cooperation", "is_simulated": True}

    def __init__(self, *, conflict=False, blocked=None):
        self.conflict, self.blocked = conflict, blocked
        self.observed = {"data": [], "document": [], "coordinator": []}

    def generate(self, system, payload, *, max_tokens):
        role = payload.get("delegation", {}).get("role", "coordinator")
        self.observed[role].append(payload)
        if role == "data":
            if self.blocked:
                self.blocked[0].set()
                assert self.blocked[1].wait(20)
            value = decision(
                "finish",
                proposal={
                    "disposition": "insufficient_evidence",
                    "claims": [],
                    "unresolved_fields": ["individual_vehicle"],
                },
            )
            if self.conflict:
                value["assessments"] = [
                    {
                        "issue": "applicability",
                        "subject": "TASK",
                        "value": "supported",
                        "support_ids": ["E001"],
                        "counter_ids": [],
                        "reason": "SYNTHETIC opposing proposal; not semantic proof",
                    }
                ]
        elif role == "document":
            texts = [e for e in payload["evidence"] if e["kind"] == "text"]
            if not payload["candidates"]:
                value = decision("search", query="brake")
            elif not texts:
                value = decision("expand", document_id="D001", include_related=False)
            else:
                value = finish(payload, counter=self.conflict)
                if self.conflict:
                    value["assessments"] = [
                        {
                            "issue": "applicability",
                            "subject": "TASK",
                            "value": "contradicted",
                            "support_ids": [texts[0]["alias"]],
                            "counter_ids": [texts[0]["alias"]],
                            "reason": "SYNTHETIC opposing proposal; individual conditions unknown",
                        }
                    ]
        else:
            ids = [
                a for e in payload["evidence"] for a in e.get("equivalent_aliases", [e["alias"]])
            ]
            text_ids = [e["alias"] for e in payload["evidence"] if e["kind"] == "text"]
            value = {
                "disposition": "answered" if text_ids else "insufficient_evidence",
                "claims": [
                    {
                        "claim_id": "C001",
                        "kind": "source_fact",
                        "statement": "SYNTHETIC bounded source fact; not an applicability confirmation",
                        "scope": payload["scope"],
                        "support_ids": text_ids,
                        "counter_ids": [a for a in ids if a not in text_ids],
                    }
                ]
                if text_ids
                else [],
                "unresolved_fields": ["individual_vehicle"],
            }
        return {
            "identity": self.identity,
            "proposal": value,
            "accepted_complete": True,
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "estimated_cost": None,
            "elapsed_ms": 0,
        }


def prepare(control, submit, *, model=None, timeout=20, enabled=True):
    ready = submit(mode="EXECUTE")
    plan = Plan.model_validate(ready["plan"])
    plan.investigator = "multi_agent"
    plan.request = plan.request.model_copy(update={"intent": "cross_source"})
    ready = control.revise("Lin", "LEARN", ready["version"], plan)
    control.model = model or TeamModel()
    control.config = FixedConfig(
        retrieval_method="bm25",
        max_model_calls=8,
        max_tool_calls=10,
        context_mode="structured",
        branch_timeout_seconds=timeout,
        multi_agent_enabled=enabled,
    )
    # Explicit one-source fixture uses an allowlisted pinned pair feature to exercise two roles.
    import investigation.cooperation as module

    return ready, module


def dispatch(control, ready):
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "LEARN", Enqueue(expected_version=ready["version"], idempotency_key="team")
    )
    return d, job


def force_two_roles(monkeypatch, module):
    original = module.trigger
    monkeypatch.setattr(
        module, "trigger", lambda request, count, config: original(request, max(2, count), config)
    )


def test_isolated_roles_merge_verified_sources_and_share_root_allowance(controlled, monkeypatch):
    control, submit, _ = controlled
    ready, module = prepare(control, submit)
    force_two_roles(monkeypatch, module)
    d, job = dispatch(control, ready)
    result = Worker(d, "team").run_once()
    assert result["task"]["state"] == "COMPLETED", result
    report = control.report("Lin", "LEARN")
    assert report["method"] == "multi_agent" and len(report["branch_results"]) == 2
    assert report["evidence_verification"]["status"] == "verified"
    assert report["model_calls"] <= 8 and len(report["tool_calls"]) <= 10
    assert report["resources"]["completion_tokens_reserved_or_used"] <= 4000
    assert all(
        "branch_summaries" not in p
        for role in ("data", "document")
        for p in control.model.observed[role]
    )
    assert len(control.model.observed["coordinator"]) == 1
    assert "search" not in control.model.observed["data"][0]["available_tools"]
    assert "links" not in control.model.observed["document"][0]["available_tools"]
    assert sum(t["tool"] == "lookup" for t in report["tool_calls"]) == 1
    assert len(report["cards"]) == len({c["evidence_id"] for c in report["cards"]})
    seed = report["seed_evidence_reused_by_both_branches"]
    assert len(seed) == 1
    assert all(seed[0] in b["evidence_ids"] for b in report["branch_results"])
    with d.catalog.engine.connect() as connection:
        ordinals = connection.scalars(
            sa.select(calls.c.ordinal).where(calls.c.job_id == job["job_id"])
        ).all()
    assert any(100 <= v < 200 for v in ordinals) and any(200 <= v < 300 for v in ordinals)


def test_opposing_applicability_proposals_preserve_both_sides_without_voting(
    controlled, monkeypatch
):
    control, submit, _ = controlled
    ready, module = prepare(control, submit, model=TeamModel(conflict=True))
    force_two_roles(monkeypatch, module)
    d, _ = dispatch(control, ready)
    result = Worker(d, "conflicts").run_once()
    assert result["task"]["state"] == "COMPLETED", result
    report = control.report("Lin", "LEARN")
    assert report["conflicts"] and report["proposal"]["disposition"] == "insufficient_evidence"
    assert "individual_vehicle" in report["proposal"]["unresolved_fields"]
    assert "未消解" in report["answer"]
    assert report["evidence_verification"]["origin_replays"] >= 2


def test_document_branch_progresses_while_data_branch_is_blocked_then_timeout_keeps_success(
    controlled, monkeypatch
):
    control, submit, _ = controlled
    entered, release = Event(), Event()
    model = TeamModel(blocked=(entered, release))
    ready, module = prepare(control, submit, model=model, timeout=2)
    force_two_roles(monkeypatch, module)
    d, _ = dispatch(control, ready)
    try:
        result = Worker(d, "partial").run_once()
        assert entered.is_set()
        assert model.observed["document"] and model.observed["coordinator"]
        assert result["task"]["state"] == "COMPLETED", result
        report = control.report("Lin", "LEARN")
        assert {b["status"] for b in report["branch_results"]} == {"completed", "timeout"}
        assert report["proposal"]["disposition"] == "insufficient_evidence" and report["cards"]
    finally:
        release.set()
        # Let the late callback settle before the fixture drops its database schema.
        for _ in range(200):
            import time

            with d.catalog.engine.connect() as connection:
                left = connection.scalar(
                    sa.select(sa.func.count()).select_from(calls).where(calls.c.state == "unknown")
                )
            if not left:
                break
            time.sleep(0.01)


def test_disabled_and_simple_tasks_fall_back_without_creating_branches(controlled):
    from tests.test_single_agent import ScriptedAgent

    control, submit, _ = controlled
    ready, _ = prepare(control, submit, enabled=False)
    control.model = ScriptedAgent(
        [decision("search", query="brake"), decision("expand", document_id="D001"), finish]
    )
    d, _ = dispatch(control, ready)
    result = Worker(d, "fallback").run_once()
    assert result["task"]["state"] == "COMPLETED", result
    report = control.report("Lin", "LEARN")
    assert report["method"] == "single_agent" and not report["delegation_policy"]["delegate"]


def test_parent_cancel_stops_both_branches_and_blocks_formal_delivery(controlled, monkeypatch):
    control, submit, _ = controlled
    entered, release = Event(), Event()
    ready, module = prepare(control, submit, model=TeamModel(blocked=(entered, release)))
    force_two_roles(monkeypatch, module)
    d, _ = dispatch(control, ready)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Worker(d, "cancelled").run_once)
            assert entered.wait(20)
            state = control.view("Lin", "LEARN")
            control.change_state("Lin", "LEARN", state["version"], "cancel")
            release.set()
            result = future.result(timeout=30)
        assert result["task"]["state"] == "CANCELLED" and result["task"]["published_report"] is None
    finally:
        release.set()


def parallel_ready(control, submit, steps):
    ready = submit(mode="EXECUTE")
    plan = Plan.model_validate(ready["plan"])
    plan.investigator = "parallel_tools"
    with control.catalog.engine.connect() as connection:
        from database.schema import source_snapshots

        snapshot = connection.scalar(sa.select(source_snapshots.c.snapshot_id).limit(1))
    plan.request = plan.request.model_copy(
        update={
            "intent": "relationship",
            "pairs": [
                {
                    "left": {"snapshot_id": snapshot, "ordinal": 0},
                    "right": {"snapshot_id": snapshot, "ordinal": 0},
                }
            ],
        }
    )
    plan = Plan.model_validate(plan.model_dump(mode="json"))
    ready = control.revise("Lin", "LEARN", ready["version"], plan)
    control.model = ScriptedAgent(steps)
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=8, max_tool_calls=10, context_mode="structured"
    )
    return ready


def test_parallel_tools_are_concurrent_calls_with_one_shared_planner_context(
    controlled, monkeypatch
):
    control, submit, _ = controlled
    ready = parallel_ready(
        control,
        submit,
        [
            decision("batch", batch=[{"action": "search", "query": "brake"}, {"action": "links"}]),
            decision("expand", document_id="D001", include_related=False),
            finish,
        ],
    )
    entered_search, entered_links = Event(), Event()
    query = control.service.query

    def searching(*args, **kwargs):
        if not kwargs:
            entered_search.set()
            assert entered_links.wait(5)
        return query(*args, **kwargs)

    def linking(*args, **kwargs):
        entered_links.set()
        assert entered_search.wait(5)
        # This fixture has one record. Supply an explicit unsupported relation result;
        # the test measures concurrent dispatch, not a real relation verdict.
        return {
            "status": "unsupported",
            "assessment": None,
            "task_id": args[0].task_id,
            "release_id": args[0].expected_release_id,
            "unknown_conditions": ["SYNTHETIC pair unavailable"],
        }

    monkeypatch.setattr(control.service, "query", searching)
    monkeypatch.setattr(control.service, "links", linking)
    d, _ = dispatch(control, ready)
    result = Worker(d, "parallel").run_once()
    assert result["task"]["state"] == "COMPLETED", result
    report = control.report("Lin", "LEARN")
    assert report["method"] == "single_agent_parallel_tools" and len(report["tool_batches"]) == 1
    assert report["tool_batches"][0]["shared_planner_context"]
    assert not any("delegation" in p for p in control.model.inputs)


def test_parallel_batch_cannot_expand_a_result_that_is_not_yet_available(controlled):
    control, submit, _ = controlled
    ready = parallel_ready(
        control,
        submit,
        [
            decision(
                "batch",
                batch=[
                    {"action": "search", "query": "brake"},
                    {"action": "expand", "document_id": "D001"},
                ],
            )
        ],
    )
    d, job = dispatch(control, ready)
    result = Worker(d, "dependent").run_once()
    assert result["task"]["state"] == "FAILED"
    status = d.view("Lin", "LEARN")
    assert sum(c["kind"] == "tool" for c in status["calls"]) == 1


def test_delegated_tools_are_enforced_in_code_and_successful_sibling_is_retained(
    controlled, monkeypatch
):
    control, submit, _ = controlled
    model = TeamModel()
    original = model.generate

    def overreach(system, payload, *, max_tokens):
        response = original(system, payload, max_tokens=max_tokens)
        if payload.get("delegation", {}).get("role") == "data":
            response["proposal"] = decision("search", query="outside my tools")
        return response

    model.generate = overreach
    ready, module = prepare(control, submit, model=model)
    force_two_roles(monkeypatch, module)
    d, _ = dispatch(control, ready)
    assert Worker(d, "rejected-role").run_once()["task"]["state"] == "COMPLETED"
    report = control.report("Lin", "LEARN")
    assert {r["status"] for r in report["branch_results"]} == {"failed", "completed"}
    assert not any(t["role"] == "data" and t["tool"] == "search" for t in report["tool_calls"])
    assert report["proposal"]["disposition"] == "insufficient_evidence"


def test_parallel_one_tool_timeout_preserves_the_other_tool_output(controlled, monkeypatch):
    control, submit, _ = controlled
    abstain = decision(
        "finish",
        proposal={
            "disposition": "insufficient_evidence",
            "claims": [],
            "unresolved_fields": ["causality"],
        },
    )
    ready = parallel_ready(
        control,
        submit,
        [
            decision("batch", batch=[{"action": "search", "query": "brake"}, {"action": "links"}]),
            decision("expand", document_id="D001", include_related=False),
            abstain,
        ],
    )

    def timeout(*args, **kwargs):
        raise TimeoutError("SYNTHETIC readonly timeout")

    monkeypatch.setattr(control.service, "links", timeout)
    d, _ = dispatch(control, ready)
    assert Worker(d, "partial-tool").run_once()["task"]["state"] == "COMPLETED"
    report = control.report("Lin", "LEARN")
    assert any(c["kind"] == "text" for c in report["cards"])
    assert report["proposal"]["disposition"] == "insufficient_evidence"
    assert any(o.get("result_is_not_zero") for o in report["observations"])


def test_uncertain_branch_model_call_does_not_pause_successful_sibling_or_release_budget(
    controlled, monkeypatch
):
    from models.chat_api import ModelCallFailed

    control, submit, _ = controlled
    model = TeamModel()
    original = model.generate

    def lost_response(system, payload, *, max_tokens):
        if payload.get("delegation", {}).get("role") == "data":
            raise ModelCallFailed("model_transport_failed")
        return original(system, payload, max_tokens=max_tokens)

    model.generate = lost_response
    ready, module = prepare(control, submit, model=model)
    force_two_roles(monkeypatch, module)
    d, _ = dispatch(control, ready)
    result = Worker(d, "partial-model").run_once()
    assert result["task"]["state"] == "COMPLETED", result
    report = control.report("Lin", "LEARN")
    assert {r["status"] for r in report["branch_results"]} == {"failed", "completed"}
    assert report["proposal"]["disposition"] == "insufficient_evidence"
    pending = [c for c in d.view("Lin", "LEARN")["calls"] if c["state"] == "unknown"]
    assert (
        len(pending) == 1
        and pending[0]["pending_reconciliation"]
        and pending[0]["charged"]["completion_tokens"] > 0
    )


def test_multi_agent_recovery_replays_both_lanes_and_coordinator_under_new_epoch(
    controlled, monkeypatch
):
    from control.worker import InjectedCrash
    from tests.test_dispatch_postgres import expire, make_ready

    control, submit, _ = controlled
    ready, module = prepare(control, submit)
    force_two_roles(monkeypatch, module)
    d, job = dispatch(control, ready)
    claim = d.claim("before-crash")

    def fault(stage, kind, ordinal):
        if stage == "after_settle" and kind == "model" and ordinal == 1:
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(d, "before-crash", fault=fault, heartbeat=False).run_claim(claim)
    observed = {k: len(v) for k, v in control.model.observed.items()}
    expire(d, claim)
    assert d.recover_expired() == [job["job_id"]]
    make_ready(d, job["job_id"])
    result = Worker(d, "recovered", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED", result
    assert result["calls"]["new_model_calls"] == 0
    assert result["calls"]["replayed_model_calls"] == sum(observed.values())
    assert observed == {k: len(v) for k, v in control.model.observed.items()}
    report = control.report("Lin", "LEARN")
    assert all(d["epoch"] > claim.access.epoch for d in report["delegations"])


def test_parallel_batch_saved_outputs_replay_without_repeating_the_batch(controlled, monkeypatch):
    from control.worker import InjectedCrash
    from tests.test_dispatch_postgres import expire, make_ready

    control, submit, _ = controlled
    ready = parallel_ready(
        control,
        submit,
        [
            decision("batch", batch=[{"action": "search", "query": "brake"}, {"action": "links"}]),
            decision("expand", document_id="D001", include_related=False),
            finish,
        ],
    )
    # Same explicit unsupported synthetic pair result as the dispatch-concurrency test.
    count = []

    def link(*args, **kwargs):
        count.append(1)
        return {"status": "unsupported", "assessment": None}

    monkeypatch.setattr(control.service, "links", link)
    d, job = dispatch(control, ready)
    claim = d.claim("before-batch-crash")

    def fault(stage, kind, ordinal):
        if stage == "after_settle" and kind == "model" and ordinal == 2:
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(d, "before-batch-crash", fault=fault, heartbeat=False).run_claim(claim)
    expire(d, claim)
    d.recover_expired()
    make_ready(d, job["job_id"])
    result = Worker(d, "recovered-batch", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED", result
    # Evidence integrity replay can read links again. It must not dispatch/charge the
    # saved batch again: only the later expansion is a newly journaled tool call.
    assert count
    assert result["calls"]["new_tool_calls"] == 1
    assert result["calls"]["replayed_tool_calls"] == 3
    assert result["calls"]["replayed_model_calls"] == 2


def test_durable_lane_quota_cannot_borrow_unused_sibling_quota(controlled):
    from control.journal import Journal
    from control.queue_contracts import vector
    from models.chat_api import ModelCallFailed

    control, submit, _ = controlled
    ready, _ = prepare(control, submit)
    d, _ = dispatch(control, ready)
    claim = d.claim("quota")
    journal = Journal(d, claim, control.model)
    data = journal.fork(1, allocation=vector(tool_calls=1))
    document = journal.fork(2, allocation=vector(tool_calls=1))
    assert data.tool("one", {}, lambda: {"ok": True})["ok"]
    with pytest.raises(ModelCallFailed, match="delegated_resource_budget_exhausted"):
        data.tool("two", {}, lambda: pytest.fail("must not execute"))
    assert document.tool("one", {}, lambda: {"ok": True})["ok"]
    assert d.view("Lin", "LEARN")["resources"]["used"]["tool_calls"] == 2

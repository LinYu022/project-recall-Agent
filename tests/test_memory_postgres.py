"""Real DB/auth/cache/worker tests. Source texts, extraction and generation are synthetic."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.control_api import create_control_app
from control.access import AccessDenied, ControlConflict
from control.contracts import Plan
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.worker import Worker
from ingestion.records import content_hash
from memory import schema as db
from memory.contracts import (
    CandidateInput,
    Change,
    Edit,
    ProjectInput,
    Proposal,
    Review,
    Search,
    Selection,
    Settings,
    SourceInput,
)
from memory.service import MemoryService
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import TOKEN, approval_for
from tests.test_control_postgres import controlled as controlled

pytestmark = pytest.mark.postgres


@pytest.fixture
def remembered(controlled):
    control, submit, bundle = controlled
    memory = control.memory
    memory.project("Lin", ProjectInput(project_id="recall", name="SYNTHETIC recall"))
    return control, submit, bundle, memory


def proposal(**changes):
    return Proposal.model_validate(
        {
            "kind": "preference",
            "scope": "project",
            "project_id": "recall",
            "semantic_key": "response.language",
            "value": "SYNTHETIC 中文",
        }
        | changes
    )


def candidate(memory, p=None, *, owner="Lin", origin="user_statement"):
    p = p or proposal()
    source = memory.source(
        owner,
        SourceInput(
            project_id=p.project_id, origin=origin, text="SYNTHETIC explicit preference declaration"
        ),
    )
    return memory.candidate(owner, CandidateInput(source_id=source["source_id"], proposal=p))


def approve(memory, c, *, owner="Lin", replace=None):
    return memory.review(
        owner,
        c["candidate_id"],
        Review(
            expected_revision=c["revision"],
            proposal_sha256=c["proposal_sha256"],
            decision="approve",
            note="SYNTHETIC explicit user confirmation",
            replace_memory_id=replace,
        ),
    )


def request(bundle, **changes):
    return Search.model_validate(
        {
            "project_id": "recall",
            "release_id": bundle["manifest"]["release_id"],
            "allowed_source_ids": bundle["manifest"]["allowed_source_ids"],
            "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            "query": "response language 中文",
        }
        | changes
    )


def ready_with_memory(control, submit, *, name="LEARN", method="fixed"):
    first = submit(name=name, mode="EXECUTE")
    plan = Plan.model_validate(first["plan"])
    plan.memory = Selection(project_id="recall")
    plan.investigator = method
    pending = control.revise("Lin", name, first["version"], plan)
    assert pending["state"] == "WAITING_APPROVAL"
    return control.approve("Lin", name, approval_for(pending))


def test_candidate_requires_exact_user_review_and_never_enters_search_early(remembered):
    _, _, bundle, m = remembered
    c = candidate(m)
    assert not m.search("Lin", request(bundle))["entries"]
    with pytest.raises(ControlConflict):
        m.review(
            "Lin",
            c["candidate_id"],
            Review(
                expected_revision=1, proposal_sha256="0" * 64, decision="approve", note="SYNTHETIC"
            ),
        )
    done = approve(m, c)
    entries = m.search("Lin", request(bundle))["entries"]
    assert entries[0]["memory_id"] == done["memory_id"]
    assert entries[0]["is_business_evidence"] is False
    with pytest.raises(ControlConflict):
        approve(m, c)


def test_conflict_and_edit_need_explicit_replacement_of_unchanged_revision(remembered):
    _, _, bundle, m = remembered
    first = approve(m, candidate(m))["memory_id"]
    c = candidate(m, proposal(value="SYNTHETIC English"))
    with pytest.raises(ControlConflict):
        approve(m, c)
    assert m.search("Lin", request(bundle))["entries"][0]["memory_id"] == first
    approve(m, c, replace=first)
    assert m.item("Lin", first)["state"] == "superseded"
    current = m.search("Lin", request(bundle))["entries"][0]["memory_id"]
    source = m.source(
        "Lin",
        SourceInput(project_id="recall", origin="user_statement", text="SYNTHETIC correction"),
    )
    edit = m.edit(
        "Lin",
        current,
        Edit(
            expected_revision=1,
            source_id=source["source_id"],
            proposal=proposal(value="SYNTHETIC concise Chinese"),
        ),
    )
    m.change(
        "Lin",
        current,
        Change(expected_revision=1, action="disable", note="SYNTHETIC changed before review"),
    )
    with pytest.raises(ControlConflict):
        approve(m, edit, replace=current)


def test_delete_clears_index_cache_and_stale_cache_cannot_resurrect_memory(remembered):
    _, _, bundle, m = remembered
    memory_id = approve(m, candidate(m))["memory_id"]
    q = request(bundle)
    assert m.search("Lin", q)["entries"] and m.search("Lin", q)["cache_hit"]
    with m.catalog.engine.connect() as c:
        stale = dict(c.execute(sa.select(db.cache)).mappings().one())
    m.change(
        "Lin",
        memory_id,
        Change(expected_revision=1, action="delete", note="SYNTHETIC wrong preference"),
    )
    generation = m.settings("Lin")["generation"]
    stale["cache_key"] = content_hash(["Lin", generation, q.model_dump(mode="json")])
    stale["generation"] = generation
    with m.catalog.engine.begin() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(db.index)) == 0
        assert c.scalar(sa.select(sa.func.count()).select_from(db.cache)) == 0
        c.execute(db.cache.insert().values(**stale))
    found = m.search("Lin", q)
    assert not found["entries"] and found["cache_hit"]
    assert [e["action"] for e in m.history("Lin", memory_id)] == ["promoted", "deleted"]
    with pytest.raises(ControlConflict):
        m.change(
            "Lin", memory_id, Change(expected_revision=2, action="disable", note="cannot resurrect")
        )


@pytest.mark.parametrize("project", [None, "recall"])
def test_namespace_and_project_disable_take_effect_on_cached_hits(remembered, project):
    _, _, bundle, m = remembered
    approve(m, candidate(m))
    assert m.search("Lin", request(bundle))["entries"]
    state = m.settings("Lin")
    disabled = m.settings(
        "Lin", Settings(expected_generation=state["generation"], project_id=project, enabled=False)
    )
    assert not m.search("Lin", request(bundle))["entries"]
    m.settings(
        "Lin",
        Settings(expected_generation=disabled["generation"], project_id=project, enabled=True),
    )
    assert m.search("Lin", request(bundle))["entries"]


def test_scope_is_filtered_before_ranking_and_other_owners_cannot_read_ids(remembered):
    _, _, bundle, m = remembered
    m.project("Other", ProjectInput(project_id="recall", name="SYNTHETIC other user"))
    m.project("Lin", ProjectInput(project_id="different", name="SYNTHETIC other project"))
    foreign = approve(m, candidate(m, owner="Other"), owner="Other")["memory_id"]
    approve(m, candidate(m, proposal(project_id="different")))
    user = approve(m, candidate(m, proposal(scope="user", project_id=None)))["memory_id"]
    assert [e["memory_id"] for e in m.search("Lin", request(bundle, limit=1))["entries"]] == [user]
    with pytest.raises(AccessDenied):
        m.item("Lin", foreign)
    with pytest.raises(AccessDenied):
        m.search("OtherWithoutProject", request(bundle))


@pytest.mark.parametrize(
    "kind,origin",
    [
        ("official_fact", "user_statement"),
        ("hypothesis", "user_statement"),
        ("preference", "untrusted_text"),
    ],
)
def test_repeated_summaries_and_fact_candidates_never_self_promote(remembered, kind, origin):
    _, _, bundle, m = remembered
    p = proposal(kind=kind, value="SYNTHETIC repeated incorrect claim")
    source = m.source(
        "Lin",
        SourceInput(
            project_id="recall",
            origin=origin,
            text=json.dumps({"memories": [p.model_dump(mode="json")] * 3}),
        ),
    )
    job = m.enqueue("Lin", source["source_id"])
    assert m.enqueue("Lin", source["source_id"])["job_id"] == job["job_id"]
    result = m.extract_once()
    assert result["state"] == "completed" and len(set(result["candidate_ids"])) == 1
    c = m.candidates("Lin")[0]
    with pytest.raises(ValueError):
        approve(m, c)
    assert not m.search("Lin", request(bundle))["entries"]


def test_extraction_worker_is_atomic_retryable_and_only_creates_candidates(remembered, monkeypatch):
    _, _, bundle, m = remembered
    declarations = [
        proposal(semantic_key="response." + key).model_dump(mode="json")
        for key in ["language", "detail"]
    ]
    source = m.source(
        "Lin",
        SourceInput(
            project_id="recall",
            origin="user_statement",
            text=json.dumps({"memories": declarations}),
        ),
    )
    job = m.enqueue("Lin", source["source_id"])
    original, count = m._candidate, []

    def crash(*args, **kwargs):
        count.append(1)
        if len(count) == 2:
            raise RuntimeError("SYNTHETIC worker crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(m, "_candidate", crash)
    with pytest.raises(RuntimeError):
        m.extract_once()
    assert not m.candidates("Lin") and m.extraction("Lin", job["job_id"])["state"] == "queued"
    monkeypatch.setattr(m, "_candidate", original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: MemoryService(m.catalog).extract_once(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(m.candidates("Lin")) == 2 and not m.search("Lin", request(bundle))["entries"]


def test_invalid_extraction_is_failed_instead_of_successful_empty(remembered):
    _, _, _, m = remembered
    s = m.source(
        "Lin",
        SourceInput(
            project_id="recall", origin="untrusted_text", text="SYNTHETIC not a declaration"
        ),
    )
    m.enqueue("Lin", s["source_id"])
    assert m.extract_once()["state"] == "failed"


def test_rule_is_checked_version_bound_and_source_access_filtered(remembered):
    _, _, bundle, m = remembered
    p = proposal(
        kind="rule",
        value="",
        semantic_key="investigation.applicability",
        release_id=bundle["manifest"]["release_id"],
        sources=[{"source_id": bundle["manifest"]["allowed_source_ids"][0]}],
        policy_id="keep_applicability_unknown",
    )
    memory_id = approve(m, candidate(m, p))["memory_id"]
    item = m.item("Lin", memory_id)
    assert all(item["validation"]["policy_checks"]["checks"].values())
    assert m.search("Lin", request(bundle))["entries"]
    assert not m.search("Lin", request(bundle, release_id="a" * 64))["entries"]
    assert not m.search("Lin", request(bundle, allowed_source_ids=[]))["entries"]
    bad = candidate(m, p.model_copy(update={"sources": [{"source_id": "UNKNOWN"}]}))
    with pytest.raises(AccessDenied):
        approve(m, bad)


def test_version_bound_rule_is_updated_through_review_for_a_new_release(
    remembered, release_factory
):
    from tests.test_release_postgres import publish

    control, _, bundle, m = remembered
    old = proposal(
        kind="rule",
        value="",
        semantic_key="investigation.applicability",
        release_id=bundle["manifest"]["release_id"],
        sources=[{"source_id": bundle["manifest"]["allowed_source_ids"][0]}],
        policy_id="keep_applicability_unknown",
    )
    old_id = approve(m, candidate(m, old))["memory_id"]
    second = release_factory("P1-13-new-version")
    publish(control.catalog, second, expected=bundle["manifest"]["release_id"])
    q = request(second)
    assert not m.search("Lin", q)["entries"]
    new = Proposal.model_validate(
        old.model_dump(mode="json")
        | {
            "release_id": second["manifest"]["release_id"],
            "sources": [{"source_id": second["manifest"]["allowed_source_ids"][0]}],
        }
    )
    source = m.source(
        "Lin",
        SourceInput(
            project_id="recall", origin="user_statement", text="SYNTHETIC verified rule update"
        ),
    )
    c = m.edit(
        "Lin", old_id, Edit(expected_revision=1, source_id=source["source_id"], proposal=new)
    )
    approve(m, c, replace=old_id)
    assert m.search("Lin", q)["entries"][0]["release_id"] == second["manifest"]["release_id"]
    assert m.item("Lin", old_id)["state"] == "superseded"


def test_pointer_version_and_tampered_provenance_are_rejected(remembered):
    from runtime.artifacts import ArtifactIntegrityError

    _, _, bundle, m = remembered
    p = proposal(
        kind="source_pointer",
        value="",
        semantic_key="source.entry",
        release_id=bundle["manifest"]["release_id"],
        sources=[
            {
                "source_id": bundle["manifest"]["allowed_source_ids"][0],
                "source_version_id": "0" * 64,
            }
        ],
    )
    with pytest.raises(ValueError):
        approve(m, candidate(m, p))
    c = candidate(m)
    memory_id = approve(m, c)["memory_id"]
    with m.catalog.engine.begin() as connection:
        connection.execute(
            db.sources.update()
            .where(db.sources.c.source_id == c["source_id"])
            .values(text="SYNTHETIC altered source")
        )
    with pytest.raises(ArtifactIntegrityError):
        m.search("Lin", request(bundle))
    assert memory_id


def test_expiry_uses_database_clock_and_removes_active_derived_data(remembered, monkeypatch):
    _, _, bundle, m = remembered
    expires = datetime.now(UTC) + timedelta(days=1)
    memory_id = approve(m, candidate(m, proposal(expires_at=expires)))["memory_id"]
    assert m.search("Lin", request(bundle))["entries"]
    monkeypatch.setattr("memory.service.now", lambda c: expires + timedelta(seconds=1))
    assert not m.search("Lin", request(bundle))["entries"]
    assert m.item("Lin", memory_id)["state"] == "expired"


def test_two_concurrent_reviews_promote_only_once(remembered):
    _, _, _, m = remembered
    c = candidate(m)

    def reviewing(_):
        try:
            return approve(m, c)["state"]
        except ControlConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reviewing, range(2))) == ["approved", "conflict"]


def test_authenticated_http_memory_owners_and_unknown_fields_are_rejected(remembered):
    control, _, bundle, m = remembered
    memory_id = approve(m, candidate(m))["memory_id"]
    token2 = "synthetic-second-owner-token-24-characters"
    client = TestClient(create_control_app(control, {TOKEN: "Lin", token2: "Other"}))
    assert client.get("/memory/settings").status_code == 403
    headers = {"Authorization": "Bearer " + TOKEN}
    assert client.post(
        "/memory/search", headers=headers, json=request(bundle).model_dump(mode="json")
    ).json()["entries"]
    assert (
        client.get(
            "/memory/items/" + memory_id, headers={"Authorization": "Bearer " + token2}
        ).status_code
        == 403
    )
    response = client.post(
        "/memory/candidates",
        headers=headers,
        json={
            "owner_id": "Other",
            "source_id": "x",
            "proposal": proposal().model_dump(mode="json"),
        },
    )
    assert response.status_code == 422 and "Other" not in response.text


def test_task_adopts_reviewed_memory_and_old_report_marks_later_deletion(remembered):
    control, submit, _, m = remembered
    memory_id = approve(m, candidate(m))["memory_id"]
    inputs = []
    original = control.model.generate

    def capture(system, payload, **kwargs):
        inputs.append(payload)
        return original(system, payload, **kwargs)

    control.model.generate = capture
    ready = ready_with_memory(control, submit)
    result = control.execute("Lin", "LEARN", ready["version"])
    assert result["state"] == "COMPLETED", result
    assert inputs[0]["memory_context"]["references"][0]["memory_id"] == memory_id
    report = control.report("Lin", "LEARN")
    assert report["memory_usage"]["references"] and not any(
        c["evidence_id"] == memory_id for c in report["cards"]
    )
    m.change(
        "Lin", memory_id, Change(expected_revision=1, action="delete", note="SYNTHETIC correction")
    )
    assert control.report("Lin", "LEARN")["memory_audit"][0]["current_state"] == "deleted"
    second = ready_with_memory(control, submit, name="NEW")
    assert control.execute("Lin", "NEW", second["version"])["state"] == "COMPLETED"
    assert not inputs[-1]["memory_context"]["references"]


def test_revocation_during_model_generation_blocks_publication_and_keeps_private_audit(remembered):
    control, submit, _, m = remembered
    memory_id = approve(m, candidate(m))["memory_id"]
    ready = ready_with_memory(control, submit)
    entered, release = Event(), Event()
    original = control.model.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    control.model.generate = blocked
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "LEARN", Enqueue(expected_version=ready["version"], idempotency_key="memory")
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Worker(d, "memory-revoked").run_once, job_id=job["job_id"])
            assert entered.wait(20)
            m.change(
                "Lin",
                memory_id,
                Change(expected_revision=1, action="delete", note="SYNTHETIC in-flight deletion"),
            )
            release.set()
            result = future.result(timeout=30)
        assert result["task"]["state"] == "PAUSED" and result["task"]["published_report"] is None
        with m.catalog.engine.connect() as c:
            assert c.scalar(sa.select(sa.func.count()).select_from(db.adoptions)) == 1
    finally:
        release.set()


@pytest.mark.parametrize("method", ["single_agent", "parallel_tools", "multi_agent"])
def test_agent_methods_receive_memory_separately_from_business_evidence(
    remembered, monkeypatch, method
):
    from investigation.contracts import FixedConfig
    from tests.test_cooperation_postgres import TeamModel
    from tests.test_single_agent import ScriptedAgent, decision, finish

    control, submit, _, m = remembered
    memory_id = approve(m, candidate(m))["memory_id"]
    ready = ready_with_memory(control, submit, method=method)
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=8, max_tool_calls=10, context_mode="structured"
    )
    if method == "multi_agent":
        import investigation.cooperation as cooperation

        original = cooperation.trigger
        monkeypatch.setattr(
            cooperation,
            "trigger",
            lambda request, count, config: original(
                request.model_copy(update={"intent": "cross_source"}), max(2, count), config
            ),
        )
        control.model = TeamModel()
    else:
        control.model = ScriptedAgent(
            [decision("search", query="brake"), decision("expand", document_id="D001"), finish]
        )
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "LEARN", Enqueue(expected_version=ready["version"], idempotency_key="memory-agent")
    )
    result = Worker(d, "memory-agent").run_once(job_id=job["job_id"])
    assert result["task"]["state"] == "COMPLETED", result
    seen = (
        [p for values in control.model.observed.values() for p in values]
        if method == "multi_agent"
        else control.model.inputs
    )
    assert seen and all(
        p["memory_context"]["references"][0]["memory_id"] == memory_id for p in seen
    )
    report = control.report("Lin", "LEARN")
    assert report["memory_usage"]["references"][0]["memory_id"] == memory_id


def test_memory_recovery_replays_saved_response_and_revoked_reference_is_not_reused(remembered):
    from control.worker import InjectedCrash
    from tests.test_dispatch_postgres import expire, make_ready

    control, submit, _, m = remembered
    memory_id = approve(m, candidate(m))["memory_id"]
    ready = ready_with_memory(control, submit)
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin",
        "LEARN",
        Enqueue(expected_version=ready["version"], idempotency_key="memory-recovery"),
    )
    claim = d.claim("old-memory")

    def crash(stage, kind, ordinal):
        if stage == "after_settle" and kind == "model":
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        Worker(d, "old-memory", fault=crash, heartbeat=False).run_claim(claim)
    expire(d, claim)
    d.recover_expired()
    make_ready(d, job["job_id"])
    result = Worker(d, "recovered-memory", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED"
    assert result["calls"]["replayed_model_calls"] == 1 and result["calls"]["new_model_calls"] == 0
    # A later task must reselect from live primary records instead of reusing a
    # historical report's adoption list. The other test asserts actual payloads.
    m.change(
        "Lin",
        memory_id,
        Change(expected_revision=1, action="disable", note="SYNTHETIC revoked after recovery"),
    )
    assert control.report("Lin", "LEARN")["memory_audit"][0]["current_state"] == "disabled"


def test_project_preference_overrides_user_slot_before_ranking_and_limiting(remembered):
    _, _, bundle, m = remembered
    personal = approve(
        m, candidate(m, proposal(scope="user", project_id=None, value="SYNTHETIC GENERAL"))
    )["memory_id"]
    project = approve(m, candidate(m, proposal(value="SYNTHETIC PROJECT")))["memory_id"]
    q = request(bundle, query="GENERAL", limit=1)
    assert [e["memory_id"] for e in m.search("Lin", q)["entries"]] == [project]
    assert m.search("Lin", q)["cache_hit"]
    m.change(
        "Lin", project, Change(expected_revision=1, action="disable", note="SYNTHETIC fallback")
    )
    assert [e["memory_id"] for e in m.search("Lin", q)["entries"]] == [personal]

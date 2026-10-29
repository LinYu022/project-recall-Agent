"""Real DB/auth/capture/navigation/worker; synthetic sources and scripted model decisions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.control_api import create_control_app
from control.access import AccessDenied
from control.contracts import Plan
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.worker import InjectedCrash, Worker
from investigation.contracts import FixedConfig
from memory import episode_schema as db
from memory.contracts import ProjectInput
from memory.episode_contracts import (
    Capture,
    EventQuery,
    HistoryChange,
    HistoryQuery,
    HistorySelection,
    SourcePage,
    Window,
)
from runtime.artifacts import ArtifactIntegrityError
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import TOKEN, approval_for
from tests.test_control_postgres import controlled as controlled
from tests.test_dispatch_postgres import expire, make_ready
from tests.test_single_agent import ScriptedAgent, decision, finish

pytestmark = pytest.mark.postgres


@pytest.fixture
def episodes(controlled):
    control, submit, bundle = controlled
    control.memory.project("Lin", ProjectInput(project_id="recall", name="SYNTHETIC history"))
    return control, submit, bundle, control.episodes


def ready(control, submit, name, *, year=2010, history=False, project="recall", method="fixed"):
    value = submit(name=name, mode="EXECUTE", model_year=year)
    plan = Plan.model_validate(value["plan"])
    plan.project_id = project
    plan.history = HistorySelection(project_id=project) if history else None
    plan.investigator = method
    waiting = control.revise("Lin", name, value["version"], plan)
    return control.approve("Lin", name, approval_for(waiting))


def negative_finish(payload):
    value = finish(payload, counter=True)
    text = next(e["alias"] for e in payload["evidence"] if e["kind"] == "text")
    value["hypotheses"] = [
        {
            "hypothesis_id": "H001",
            "statement": "SYNTHETIC same symptom proves same defect",
            "status": "contradicted",
            "support_ids": [],
            "counter_ids": [text],
        }
    ]
    return value


def make_old(control, submit, *, name="OLD", negative=False, year=2010, project="recall"):
    value = ready(
        control,
        submit,
        name,
        year=year,
        project=project,
        method="single_agent" if negative else "fixed",
    )
    if negative:
        control.config = FixedConfig(
            retrieval_method="bm25", max_model_calls=4, context_mode="structured"
        )
        control.model = ScriptedAgent(
            [
                decision("search", query="brake"),
                decision("expand", document_id="D001"),
                negative_finish,
            ]
        )
    result = control.execute("Lin", name, value["version"])
    assert result["state"] == "COMPLETED", result
    return result


def query(bundle, **changes):
    return HistoryQuery.model_validate(
        {
            "project_id": "recall",
            "release_id": bundle["manifest"]["release_id"],
            "allowed_source_ids": bundle["manifest"]["allowed_source_ids"],
            "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            "query": "brake",
        }
        | changes
    )


def test_capture_is_idempotent_and_summary_retains_refutation_without_old_answer(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit, negative=True)
    first = service.capture("Lin", Capture(task_id="OLD", project_id="recall"))
    second = service.capture("Lin", Capture(task_id="OLD", project_id="recall"))
    assert first == second
    found = service.search("Lin", query(bundle))["entries"]
    assert len(found) == 1 and found[0]["negative_hypotheses"][0]["status"] == "contradicted"
    assert found[0]["counterevidence_count"] > 0
    assert "SYNTHETIC bounded answer" not in str(found) and "proposal" not in found[0]
    with control.catalog.engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(db.episodes)) == 1


def test_same_symptom_different_year_or_project_never_occupies_top_k(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit)
    make_old(control, submit, name="WRONG-YEAR", year=2011)
    control.memory.project(
        "Lin", ProjectInput(project_id="other", name="SYNTHETIC different project")
    )
    make_old(control, submit, name="OTHER-PROJECT", project="other")
    result = service.search("Lin", query(bundle, limit=1))["entries"]
    assert [e["task_id"] for e in result] == ["OLD"]
    assert not service.search("Lin", query(bundle, scope={"make": "TOYOTA", "model": "PRIUS"}))[
        "entries"
    ]


def test_event_search_recent_pagination_and_anchor_window_preserve_order(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit, negative=True)
    first = service.search("Lin", query(bundle), recent=True)["entries"][0]
    anchor = next(e for e in first["anchors"] if e["kind"] == "hypothesis")
    window = service.window(
        "Lin",
        Window.model_validate(
            query(bundle).model_dump(mode="json")
            | {"episode_id": first["episode_id"], "event_id": anchor["event_id"], "radius": 1}
        ),
    )
    assert window["events"][1]["status"] == "contradicted"
    events = service.events(
        "Lin",
        EventQuery.model_validate(
            query(bundle, query="contradicted").model_dump(mode="json") | {"task_id": "OLD"}
        ),
    )
    assert events["events"] and all(
        e["episode_id"] == first["episode_id"] for e in events["events"]
    )
    from tests.test_investigation import FakeJSONModel

    control.model = FakeJSONModel()
    make_old(control, submit, name="SECOND")
    page = service.search("Lin", query(bundle, limit=1), recent=True)
    assert page["entries"][0]["task_id"] == "SECOND" and page["next_before_episode_id"]
    following = service.search(
        "Lin", query(bundle, limit=1, before_episode_id=page["next_before_episode_id"]), recent=True
    )
    assert following["entries"][0]["task_id"] == "OLD"
    with pytest.raises(AccessDenied):
        service.window(
            "Lin",
            Window.model_validate(
                query(bundle).model_dump(mode="json")
                | {"episode_id": first["episode_id"], "event_id": "HE-forged"}
            ),
        )


def test_original_source_expansion_replays_bytes_without_reusing_historical_claim(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit)
    entry = service.search("Lin", query(bundle))["entries"][0]
    evidence = next(c for c in entry["historical_source_handles"] if c["kind"] == "text")
    page = service.source_page(
        "Lin",
        SourcePage.model_validate(
            query(bundle).model_dump(mode="json")
            | {
                "episode_id": entry["episode_id"],
                "evidence_id": evidence["evidence_id"],
                "page_size": 50,
            }
        ),
    )
    assert page["historical_source_only"] and page["verification"]["status"] == "verified"
    assert (
        len(page["text"]) <= 50 and page["original_release_id"] == bundle["manifest"]["release_id"]
    )


def test_failed_task_is_captured_as_failure_instead_of_successful_empty(episodes):
    control, submit, bundle, service = episodes
    value = ready(control, submit, "FAILED", method="single_agent")
    control.model = ScriptedAgent([decision("expand", document_id="D999")])
    result = control.execute("Lin", "FAILED", value["version"])
    assert result["state"] == "FAILED"
    entries = service.search("Lin", query(bundle))["entries"]
    assert entries[0]["task_state"] == "FAILED" and entries[0]["failure_labels"]


def test_authentication_and_cross_owner_source_window_access_are_checked(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit)
    entry = service.search("Lin", query(bundle))["entries"][0]
    token2 = "synthetic-episode-other-user-credential"
    control.memory.project(
        "Other", ProjectInput(project_id="recall", name="SYNTHETIC same project name")
    )
    client = TestClient(create_control_app(control, {TOKEN: "Lin", token2: "Other"}))
    q = query(bundle).model_dump(mode="json")
    assert client.post("/history/search", json=q).status_code == 403
    assert not client.post(
        "/history/search", headers={"Authorization": "Bearer " + token2}, json=q
    ).json()["entries"]
    window = q | {"episode_id": entry["episode_id"], "event_id": entry["anchors"][-1]["event_id"]}
    assert (
        client.post(
            "/history/events/window", headers={"Authorization": "Bearer " + token2}, json=window
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/history/search",
            headers={"Authorization": "Bearer " + TOKEN},
            json=q | {"owner_id": "Other"},
        ).status_code
        == 422
    )


def test_revoked_source_authorization_and_disabled_episode_disappear(episodes):
    control, submit, bundle, service = episodes
    old = make_old(control, submit)
    entry = service.search("Lin", query(bundle))["entries"][0]
    service.change("Lin", entry["episode_id"], HistoryChange(expected_revision=1, enabled=False))
    assert not service.search("Lin", query(bundle))["entries"]
    service.change("Lin", entry["episode_id"], HistoryChange(expected_revision=2, enabled=True))
    assert service.search("Lin", query(bundle))["entries"]
    control.change_state("Lin", "OLD", old["version"], "revoke")
    assert not service.search("Lin", query(bundle))["entries"]


def test_event_tampering_and_missing_report_artifact_cannot_be_silently_trusted(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit)
    entry = service.search("Lin", query(bundle))["entries"][0]
    anchor = entry["anchors"][-1]
    with control.catalog.engine.begin() as c:
        c.execute(
            db.events.update()
            .where(db.events.c.event_id == anchor["event_id"])
            .values(body={"kind": "invented"})
        )
    with pytest.raises(ArtifactIntegrityError):
        service.window(
            "Lin",
            Window.model_validate(
                query(bundle).model_dump(mode="json")
                | {"episode_id": entry["episode_id"], "event_id": anchor["event_id"]}
            ),
        )
    with control.catalog.engine.connect() as c:
        saved = c.execute(
            sa.select(db.episodes.c.body).where(db.episodes.c.episode_id == entry["episode_id"])
        ).scalar_one()
    ref = saved["reports"][0]
    from pathlib import Path

    artifact = (
        Path(ref["artifact_root"]) / "artifacts" / (ref["report_artifact"]["sha256"] + ".json")
    )
    assert artifact.resolve().is_relative_to(control.root.resolve())
    artifact.unlink()
    with pytest.raises(FileNotFoundError):
        service.search("Lin", query(bundle))


def test_history_tools_record_adoption_and_current_report_keeps_only_current_evidence(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit, negative=True)

    def browse(payload):
        e = payload["history_context"]["summaries"][0]
        anchor = next(a for a in e["anchors"] if a["kind"] == "hypothesis")
        return decision(
            "history_window",
            history_episode_id=e["episode_id"],
            history_event_id=anchor["event_id"],
        )

    control.model = ScriptedAgent(
        [
            decision("history_search", history_query="brake"),
            browse,
            decision("search", query="brake"),
            decision("expand", document_id="D001"),
            finish,
        ]
    )
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=6, max_tool_calls=8, context_mode="structured"
    )
    value = ready(control, submit, "NEW", history=True, method="single_agent")
    result = control.execute("Lin", "NEW", value["version"])
    assert result["state"] == "COMPLETED", result
    report = control.report("Lin", "NEW")
    assert any(t["tool"] == "history_search" for t in report["tool_calls"])
    assert any(t["tool"] == "history_window" for t in report["tool_calls"])
    assert report["history_usage"]["references"]
    assert all(c["task_id"] == report["request"]["task_id"] for c in report["cards"])
    assert "SYNTHETIC bounded answer" not in str(control.model.inputs[0]["history_context"])
    with control.catalog.engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(db.adoptions)) == 1


def test_source_revocation_during_generation_blocks_current_publication(episodes):
    control, submit, bundle, service = episodes
    old = make_old(control, submit)
    value = ready(control, submit, "NEW", history=True)
    entered, release = Event(), Event()
    original = control.model.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    control.model.generate = blocked
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "NEW", Enqueue(expected_version=value["version"], idempotency_key="history-revoke")
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Worker(d, "blocked-history").run_once, job_id=job["job_id"])
            assert entered.wait(20)
            control.change_state("Lin", "OLD", old["version"], "revoke")
            release.set()
            result = future.result(timeout=30)
        assert result["task"]["state"] == "PAUSED" and result["task"]["published_report"] is None
    finally:
        release.set()


def test_recovery_replays_history_search_before_adopting_refs_again(episodes):
    control, submit, bundle, service = episodes
    make_old(control, submit, negative=True)
    control.model = ScriptedAgent(
        [
            decision("history_search", history_query="brake"),
            decision("search", query="brake"),
            decision("expand", document_id="D001"),
            finish,
        ]
    )
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=6, max_tool_calls=8, context_mode="structured"
    )
    value = ready(control, submit, "NEW", history=True, method="single_agent")
    d = Dispatcher(control)
    job = d.enqueue(
        "Lin", "NEW", Enqueue(expected_version=value["version"], idempotency_key="history-recover")
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
    result = Worker(d, "recovered", heartbeat=False).run_once()
    assert result["task"]["state"] == "COMPLETED", result
    assert result["calls"]["replayed_tool_calls"] >= 2
    assert result["calls"]["replayed_model_calls"] >= 1
    assert control.report("Lin", "NEW")["history_usage"]["references"]


def test_future_tasks_are_excluded_from_adoption_snapshot_and_changed_release_is_explicit(episodes):
    from datetime import UTC, datetime, timedelta

    control, submit, bundle, service = episodes
    make_old(control, submit)
    assert not service.search(
        "Lin", query(bundle, occurred_before=datetime.now(UTC) - timedelta(days=1))
    )["entries"]
    assert not service.search("Lin", query(bundle, release_id="b" * 64))["entries"]
    assert service.search("Lin", query(bundle, release_id="b" * 64, allow_cross_release=True))[
        "entries"
    ]
    assert not service.search("Lin", query(bundle, allowed_source_ids=[]))["entries"]
    value = ready(control, submit, "NEW", history=True)
    make_old(control, submit, name="FUTURE")
    result = control.execute("Lin", "NEW", value["version"])
    assert result["state"] == "COMPLETED"
    assert [s["task_id"] for s in control.report("Lin", "NEW")["history_usage"]["summaries"]] == [
        "OLD"
    ]


def test_optional_history_index_error_preserves_formal_result_and_is_explicit(
    episodes, monkeypatch
):
    control, submit, _, service = episodes

    def broken(*args):
        raise ArtifactIntegrityError("SYNTHETIC index failure")

    monkeypatch.setattr(service, "_capture", broken)
    value = ready(control, submit, "NEW")
    result = control.execute("Lin", "NEW", value["version"])
    assert result["state"] == "COMPLETED" and result["quality"]["history_index"] == "failed"
    assert any(
        e["action"] == "history_capture_failed" for e in control.history("Lin", "NEW")["events"]
    )


@pytest.mark.parametrize("method", ["fixed", "parallel_tools", "multi_agent"])
def test_all_other_methods_receive_hints_but_must_verify_current_lookup(
    episodes, monkeypatch, method
):
    from tests.test_cooperation_postgres import TeamModel
    from tests.test_investigation import FakeJSONModel

    control, submit, _, _ = episodes
    make_old(control, submit, negative=True)
    if method == "multi_agent":
        import investigation.cooperation as module

        original = module.trigger
        monkeypatch.setattr(
            module,
            "trigger",
            lambda request, count, config: original(
                request.model_copy(update={"intent": "cross_source"}), max(2, count), config
            ),
        )
        control.model = TeamModel()
    elif method == "parallel_tools":
        control.model = ScriptedAgent(
            [decision("search", query="brake"), decision("expand", document_id="D001"), finish]
        )
    else:
        control.model = FakeJSONModel()
    control.config = FixedConfig(
        retrieval_method="bm25", max_model_calls=8, max_tool_calls=10, context_mode="structured"
    )
    value = ready(control, submit, "NEW", history=True, method=method)
    result = control.execute("Lin", "NEW", value["version"])
    assert result["state"] == "COMPLETED", result
    report = control.report("Lin", "NEW")
    assert (
        report["history_usage"]["references"]
        and report["evidence_verification"]["status"] == "verified"
    )
    assert any(c["kind"] == "structured_lookup" for c in report["cards"])

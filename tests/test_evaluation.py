"""Design leakage, clustered statistics, unknown calls and honest review boundaries."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from evaluation.analysis import paired_interval, summarize
from evaluation.contracts import Protocol, cells
from evaluation.freeze import load, seal
from evaluation.records import ProviderLedger, RunRecords
from evaluation.review import apply_review
from ingestion.records import content_hash
from tests.evaluation_fixtures import protocol_data


@pytest.mark.parametrize(
    "mutation",
    [
        "unequal_sources",
        "unequal_cases",
        "same_event_other_split",
        "seen_test",
        "extra_context_factor",
    ],
)
def test_evaluation_rejects_unfair_or_leaking_design(mutation):
    data, _, _ = protocol_data()
    if mutation == "unequal_sources":
        data["cases"][0]["sources"] *= 2
    elif mutation == "unequal_cases":
        data["arms"][1]["cases"] = ["EVAL-FACTS"]
    elif mutation == "same_event_other_split":
        data["cases"][1]["split"] = "development"
    elif mutation == "seen_test":
        data["previously_seen_campaigns"].append("26V999003")
    else:
        data["arms"][4]["retrieval"] = "hybrid"
    with pytest.raises(ValidationError):
        Protocol.model_validate(data)


def test_frozen_code_prefix_and_denominator_cannot_silently_change(tmp_path, monkeypatch):
    data, prefix, _ = protocol_data()
    identity = seal(data, tmp_path / "experiments", prefix)
    body, protocol = load(identity)
    records = RunRecords(tmp_path / "runs", identity, body, mode="scripted")
    first = body["cells"][0]
    assert records.claim(first["cell_id"])
    assert not records.claim(first["cell_id"])
    records.finish(
        first, {"state": "failed", "category": "direct", "error_code": "synthetic_model_error"}
    )
    from evaluation.__main__ import observations

    rows = observations(records, protocol)
    report = summarize(protocol, rows)
    assert report["arms"]["fixed"]["frozen_tasks_and_repeats"] == 3
    assert report["arms"]["fixed"]["formal_task_completion_rate"] is None
    assert report["unresolved_cells"] == len(cells(protocol)) - 1
    with pytest.raises(ValueError):
        summarize(protocol, rows[1:])
    monkeypatch.setattr("evaluation.freeze.implementation", lambda: {})
    with pytest.raises(ValueError, match="code_changed"):
        load(identity)


def test_repeated_tasks_do_not_inflate_cluster_intervals_or_hide_missing_pairs():
    rows = []
    for group in range(5):
        for repeat in range(10):
            for arm, value in [("a", 10), ("b", 12 + group)]:
                rows.append(
                    {
                        "cell": {
                            "case_id": str(group),
                            "repeat": repeat,
                            "arm_id": arm,
                            "event_cluster_id": str(group),
                        },
                        "latency": value,
                    }
                )
    result = paired_interval(rows, "a", "b", "latency")
    assert result["paired_clusters"] == 5 and result["mean_delta"] == 4
    assert result["interval_95"] is not None
    missing = deepcopy(rows)
    missing[0]["latency"] = None
    assert paired_interval(missing, "a", "b", "latency")["interval_95"] is None
    small = [r for r in rows if r["cell"]["event_cluster_id"] == "0"]
    assert paired_interval(small, "a", "b", "latency")["paired_clusters"] == 1
    assert paired_interval(small, "a", "b", "latency")["interval_95"] is None


def test_unknown_provider_requests_keep_reserved_budget_across_restart(tmp_path):
    ledger = ProviderLedger(
        tmp_path / "ledger.json", experiment_id="x", cells={"a": 2}, identity={"model": "SYNTHETIC"}
    )
    ledger.reserve("a", "fingerprint", 2000)
    ledger = ProviderLedger(
        tmp_path / "ledger.json", experiment_id="x", cells={"a": 2}, identity={"model": "SYNTHETIC"}
    )
    ticket = ledger.reserve("a", "second", 2000)
    ledger.finish(
        ticket, artifact={"sha256": "a" * 64}, usage={"completion_tokens": 10}, state="failed"
    )
    with pytest.raises(ValueError):
        ledger.reserve("a", "third", 1)


def test_agent_draft_or_incomplete_human_review_never_becomes_formal_success(tmp_path):
    protocol, _, _ = protocol_data()
    case = Protocol.model_validate(protocol).cases[1]
    observation = {"blind_id": "BLIND-SYNTHETIC", "state": "guarded"}
    value = {
        "blind_id": observation["blind_id"],
        "observation_sha256": content_hash(observation),
        "reviewer_kind": "implementation_agent",
        "reviewer_id": "SYNTHETIC",
        "complete": True,
        "task_success": True,
        "disposition_correct": True,
        "dispute_status": "none",
        "citations": [],
        "relations": [],
        "semantic": None,
        "notes": "SYNTHETIC",
    }
    assert apply_review(observation, value, case, tmp_path)["formal_task_success"] is None
    value |= {"reviewer_kind": "human", "complete": False}
    assert apply_review(observation, value, case, tmp_path)["status"] == "pending"
    value["observation_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        apply_review(observation, value, case, tmp_path)


def test_deep_evaluation_artifacts_are_atomically_published_on_windows(tmp_path):
    from runtime.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / ("depth-" + "a" * 65) / ("depth-" + "b" * 65))
    ref = store.put_json({"SYNTHETIC": "deep experiment hierarchy"})
    assert store.read_json(ref["sha256"])["SYNTHETIC"] == "deep experiment hierarchy"
    assert store.put_json({"SYNTHETIC": "deep experiment hierarchy"}) == ref


def test_evaluation_labels_or_secret_fields_are_blocked_before_provider_reservation(tmp_path):
    from evaluation.executor import ScriptedModel
    from evaluation.records import MeteredModel
    from models.chat_api import ModelCallFailed
    from runtime.artifacts import ArtifactStore

    ledger = ProviderLedger(
        tmp_path / "ledger.json",
        experiment_id="SYNTHETIC",
        cells={"a": 1},
        identity=ScriptedModel.identity,
    )
    model = MeteredModel(ScriptedModel(), ledger, "a", ArtifactStore(tmp_path))
    with pytest.raises(ModelCallFailed):
        model.generate("SYNTHETIC", {"nested": [{"reference_sha256": "a" * 64}]}, max_tokens=10)
    assert __import__("json").loads((tmp_path / "ledger.json").read_bytes())["entries"] == []

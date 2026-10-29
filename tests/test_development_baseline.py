"""The new pilot cannot consume old permissions or disguise simulated generation."""

from copy import deepcopy
from pathlib import Path

import pytest

from baselining.preparation import ROOT, locate_atom, validate_design
from baselining.runner import PreviewStop, check_approval
from models.chat_api import ModelCallFailed
from reviewing.corpus import read
from runtime.artifacts import ArtifactStore


@pytest.mark.parametrize("change", ["old_test_source", "target_after_results", "test_split", "cap"])
def test_pilot_scope_and_unestablished_quality_target_cannot_drift(change):
    design = deepcopy(read(ROOT / "design.json"))
    if change == "old_test_source":
        design["allowed_source_ids"].append("NHTSA-EVAL-CAMPAIGN-18V332000")
    elif change == "target_after_results":
        design["quality_targets"] = {"pass_rate": 0.9}
    elif change == "test_split":
        design["split"] = "test"
    else:
        design["cases"][0]["maximum_requests"] = 2
    with pytest.raises(ValueError):
        validate_design(design)


@pytest.mark.parametrize("change", ["old_approval", "plan", "cap", "model", "no_reply"])
def test_pilot_requires_exact_new_actual_user_approval(change):
    pointer = {"plan_id": "b" * 64}
    plan = {"design": read(ROOT / "design.json")}
    approval = {
        "status": "explicit_user_approved",
        "plan_id": pointer["plan_id"],
        "maximum_requests": 3,
        "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com",
        "source_kind": "actual_human_reply",
        "human_reply": "SYNTHETIC permission fixture, never a real approval",
    }
    if change == "old_approval":
        approval["plan_id"] = "P1-16-original-batch"
        approval["maximum_requests"] = 584
    elif change == "no_reply":
        approval["human_reply"] = ""
    else:
        approval[{"plan": "plan_id", "cap": "maximum_requests", "model": "model"}[change]] = (
            "changed"
        )
    with pytest.raises(ValueError):
        check_approval(pointer, plan, approval)


def test_unapproved_live_pilot_stops_before_model_ledger_or_database(tmp_path, monkeypatch):
    import baselining.runner as runner

    monkeypatch.setattr(runner, "checked", lambda _: ({}, None, []))
    monkeypatch.setattr(runner, "APPROVAL", tmp_path / "missing-approval.json")

    def forbidden(*a, **kw):
        raise AssertionError("must not initialize a provider or database without approval")

    monkeypatch.setattr(runner, "ChatJSONModel", forbidden)
    monkeypatch.setattr(runner, "ProviderLedger", forbidden)
    monkeypatch.setattr(runner, "postgres", forbidden)
    with pytest.raises(ValueError, match="requires_explicit_approval"):
        runner.run({}, Path("unused"), mode="live")


def test_preview_persists_real_input_and_deliberately_never_returns_an_answer(tmp_path):
    model = PreviewStop(ArtifactStore(tmp_path))
    with pytest.raises(ModelCallFailed, match="stopped_before_provider"):
        model.generate("SYNTHETIC system", {"evidence": ["SYNTHETIC source"]}, max_tokens=256)
    value = model.store.read_json(model.inputs[0]["sha256"])
    assert value["payload"] == {"evidence": ["SYNTHETIC source"]}
    assert model.identity["is_simulated"] is True
    assert "proposal" not in value and "usage" not in value


def test_reference_quotes_pin_exact_original_ranges_and_keep_all_equivalent_rows():
    atom = {"source": "prius", "key": "SYNTHETIC", "field": "Summary", "quote": "ABS condition"}
    source = "NHTSA-API-CAMPAIGN-10V039000"
    documents = [
        {
            "source": {"source_id": source, "source_version_id": c * 64},
            "units": [
                {
                    "unit_id": c * 64,
                    "field_pointer": "/results/0/Summary",
                    "text": "SYNTHETIC ABS condition only",
                }
            ],
        }
        for c in "ab"
    ]
    found = locate_atom(atom, documents)
    assert len(found) == 2 and all(m["start"] == 10 and m["end"] == 23 for m in found)
    atom["quote"] = "unavailable fact"
    with pytest.raises(ValueError, match="not_found_in_original_source"):
        locate_atom(atom, documents)


def review_summary_fixture():
    rows, reviews = [], []
    for case, cluster, scope, verdict in [
        ("A", "PRIUS", "generated_answer", "satisfied"),
        ("B", "PRIUS", "generated_answer", "mostly_satisfied_with_minor_caveat"),
        ("C", "FORD", "generated_answer", "satisfied"),
        ("D", "PRIUS", "stop_or_limited_answer", "satisfied"),
    ]:
        rows.append({"cell": {"case_id": case, "event_cluster_id": cluster}})
        reviews.append(
            {
                "case_id": case,
                "review_scope": scope,
                "reviewer_id": "SYNTHETIC",
                "core_task_complete": True if scope == "generated_answer" else None,
                "all_required_conditions_verdict": verdict,
                "disposition_correct": True,
                "stop_or_limited_answer_requirements_satisfied": True
                if scope != "generated_answer"
                else None,
            }
        )
    return {"plan_id": "a" * 64, "rows": rows, "actual_human_review_records": reviews}


def test_development_target_draft_balances_events_and_keeps_stop_stratum_separate():
    from scripts.propose_development_targets import build_targets

    data = review_summary_fixture()
    result = build_targets(data)
    assert (
        result["targets"]["generated_unqualified_requirements_event_cluster_mean"][
            "development_point"
        ]
        == 0.75
    )
    assert result["targets"]["generated_core_event_cluster_mean"]["proposed_minimum"] == 1
    assert result["planned_stop_or_limited_answer_tasks"] == 1
    assert result["ready_for_full_test_freeze"] is False
    assert result["atomic_unsupported_rate_target"] is None
    assert (
        data["actual_human_review_records"][1]["all_required_conditions_verdict"]
        == "mostly_satisfied_with_minor_caveat"
    )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "failed_core"])
def test_baseline_does_not_exclude_unknown_failed_or_duplicate_cases(mutation):
    from scripts.audit_development_live import reviewed_baseline
    from scripts.propose_development_targets import build_targets

    data = review_summary_fixture()
    rows = data["actual_human_review_records"]
    cells = [r["cell"] for r in data["rows"]]
    if mutation == "missing":
        rows.pop()
        assert reviewed_baseline(rows, cells) is None
        with pytest.raises(ValueError, match="every_development_case"):
            build_targets(data)
    elif mutation == "duplicate":
        rows.append(deepcopy(rows[0]))
        with pytest.raises(ValueError, match="adjudication"):
            reviewed_baseline(rows, cells)
        with pytest.raises(ValueError, match="every_development_case"):
            build_targets(data)
    else:
        rows[0]["core_task_complete"] = False
        result = build_targets(data)
        assert result["targets"]["generated_core_event_cluster_mean"]["development_point"] == 0.75
        assert reviewed_baseline(rows, cells)["generated_core_complete"] == 2

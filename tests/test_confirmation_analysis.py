"""Synthetic integrity checks; these fixtures are never actual human grades."""

from copy import deepcopy

import pytest

from ingestion.records import content_hash
from reviewing.corpus import read, write_new
from scripts.analyze_quality_confirmation import (
    acceptance_from_witnesses,
    accounted_output,
    bind_failure_human,
    bind_human,
    bind_reading_human,
    bind_stage_human,
    generated_metric_coverage,
    load_human_index,
)
from scripts.render_confirmation_stages import render


def test_unreviewed_quality_never_passes_or_becomes_a_population_rate():
    result = acceptance_from_witnesses({"atomic_unsupported_event_cluster_mean_maximum": 0}, [])
    assert result["status"] == "not_established"
    assert result["complete_population_semantic_rates"] is None
    assert not result["passing_acceptance_allowed"]


def test_verified_zero_tolerance_violation_does_not_fill_missing_population_rate():
    rows = [
        {
            "cell_id": "test",
            "source_question_id": "synthetic-test-only",
            "atoms": [
                {
                    "atom_id": "C001-N01",
                    "verdict": "unsupported",
                    "quote": "condition",
                    "support_ids": ["E003"],
                    "basis": "missing from cited text",
                }
            ],
        }
    ]
    result = acceptance_from_witnesses({"atomic_unsupported_event_cluster_mean_maximum": 0}, rows)
    assert result["status"] == "not_passed"
    assert len(result["verified_target_violations"]) == 1
    assert result["complete_population_semantic_rates"] is None


def test_human_binding_rejects_added_cross_claim_citation(monkeypatch):
    claim = {"claim_id": "C001", "statement": "condition", "support_ids": ["E003"]}
    packet = {"blind_id": "test", "proposal": {"claims": [claim]}}
    candidate = {
        "packet_sha256": content_hash(packet),
        "claims": [
            {
                "claim": claim,
                "candidate_units": [
                    {"atom_id": "C001-A01", "start": 0, "end": 9, "quote": "condition"}
                ],
            }
        ],
    }
    observed = {"blind_id": "test"}
    source = {
        "source_kind": "actual_human_reply",
        "reviewer_kind": "human",
        "cell_id": "cell",
        "candidate_packet": "candidate",
        "source_packet": "packet",
        "blind_id": "test",
        "case_id": "case",
        "source_packet_sha256": content_hash(packet),
        "observation_sha256": content_hash(observed),
        "source_question_id": "synthetic-test-only",
        "adopted_atoms": [
            {
                "atom_id": "C001-N01",
                "navigation_ids": ["C001-A01"],
                "verdict": "unsupported",
                "support_ids": ["E003"],
            }
        ],
        "original_navigation_judgments": {"C001-A01": "unsupported"},
        "assertion_coverage_exhaustive": True,
        "obligations": {},
        "all_requirements_verdict": "partial",
        "disposition_appropriate": True,
        "core_task_complete": True,
    }
    monkeypatch.setattr(
        "scripts.analyze_quality_confirmation.read",
        lambda p: {"candidate": candidate, "packet": packet}[p],
    )
    reports = {"cell": {"proposal": packet["proposal"]}}
    assert bind_human(source, {"cell": observed}, reports)["atom_counts"] == {"unsupported": 1}
    changed = deepcopy(source)
    changed["adopted_atoms"][0]["support_ids"] = ["E002"]
    with pytest.raises(ValueError, match="adds_uncited_evidence"):
        bind_human(changed, {"cell": observed}, reports)


def test_stage_reading_keeps_actual_insufficiency_text(tmp_path):
    snapshot = {
        "experiment_id": "synthetic-test-only",
        "stage_groups": [
            {
                "group_id": "group",
                "cell_ids": ["cell"],
                "observation_sha256s": ["hash"],
                "representative_packet": {
                    "execution_failed": False,
                    "stage_obligations": [
                        {"obligation_id": "reason", "statement": "explain missing data"}
                    ],
                    "question": "rate?",
                    "scope": {},
                    "task_state": "COMPLETED",
                    "proposal": {"disposition": "insufficient_evidence"},
                    "actual_answer": "本次允许资料不足以回答问题。",
                    "evidence": [],
                },
            }
        ],
    }
    path, output = tmp_path / "snapshot.json", tmp_path / "reading.md"
    write_new(path, snapshot)
    assert render(path, output) == {"groups": 1, "cells": 1}
    assert "本次允许资料不足以回答问题。" in output.read_text(encoding="utf-8")
    assert read(output.with_suffix(".json"))["human_judgment"] is None


def test_stage_human_batch_applies_only_to_exact_explicitly_reviewed_group(monkeypatch):
    first, second = {"state": "guarded"}, {"state": "completed"}
    reading = {
        "groups": [
            {
                "review_id": "S01",
                "group_id": "g1",
                "cell_ids": ["first"],
                "observation_sha256s": [content_hash(first)],
            },
            {
                "review_id": "S02",
                "group_id": "g2",
                "cell_ids": ["second"],
                "observation_sha256s": [content_hash(second)],
            },
        ]
    }
    source = {
        "source_kind": "actual_human_reply",
        "reviewer_kind": "human",
        "reading_json": "reading",
        "reading_sha256": content_hash(reading),
        "source_question_id": "synthetic-test-only",
        "group_judgments": [{"review_id": "S01", "disposition_verdict": "partial"}],
    }
    monkeypatch.setattr("scripts.analyze_quality_confirmation.read", lambda _: reading)
    rows = bind_stage_human(source, {"first": first, "second": second})
    assert [r["cell_id"] for r in rows] == ["first"]
    with pytest.raises(ValueError, match="observation_changed"):
        bind_stage_human(source, {"first": second})


def test_unknown_usage_consumes_reserved_output_instead_of_zero():
    assert accounted_output({"usage": None, "max_tokens": 2000}) == 2000
    assert accounted_output({"usage": {"completion_tokens": None}, "max_tokens": 2000}) == 2000
    assert accounted_output({"usage": {"completion_tokens": 300}, "max_tokens": 2000}) == 300


def test_partial_stage_witness_keeps_correct_disposition_and_missing_rate_separate():
    targets = {"stop_stage_appropriateness_minimum": 1}
    rows = [
        {
            "source_question_id": "synthetic-test-only",
            "review_id": "S01",
            "cell_id": "cell",
            "disposition_verdict": "partially_appropriate",
            "disposition_code_or_guard_direction_appropriate": True,
            "reason": "Missing explanation",
        }
    ]
    result = acceptance_from_witnesses(targets, [], rows)
    assert result["status"] == "not_passed"
    assert result["verified_target_violations"][0]["cell_ids"] == ["cell"]
    assert result["complete_population_semantic_rates"] is None


def test_actual_recheck_finding_does_not_invent_atomic_category():
    reading = {
        "cell_id": "cell",
        "atomic_grades_complete": False,
        "unambiguous_adjudicated_findings": [
            {
                "finding": "does_not_directly_support",
                "navigation_id": "C005-A02",
                "evidence_alias": "E002",
                "source_question_id": "synthetic-test-only",
            }
        ],
    }
    result = acceptance_from_witnesses(
        {"named_aspect_citation_support_minimum": 1}, [], (), [reading]
    )
    assert result["status"] == "not_passed"
    assert (
        result["verified_target_violations"][0]["metric"] == "named_aspect_citation_support_minimum"
    )
    assert result["complete_population_semantic_rates"] is None


def test_initial_citation_witness_survives_without_other_reading_rows():
    row = {
        "cell_id": "cell",
        "source_question_id": "synthetic-test-only",
        "atoms": [],
        "citation_roles": [{"role": "does_not_support", "atom_ids": ["C001-N01"], "alias": "E001"}],
    }
    result = acceptance_from_witnesses({"named_aspect_citation_support_minimum": 1}, [row])
    assert len(result["verified_target_violations"]) == 1


def test_reading_receipt_binds_initial_and_revised_sources_without_atomic_imputation(monkeypatch):
    packet = {"blind_id": "blind", "proposal": {"claims": []}}
    observed = {"blind_id": "blind"}
    initial = {
        "cell_id": "cell",
        "source_kind": "actual_human_reply",
        "source_question_id": "initial",
        "core_task_complete": True,
    }
    revised = initial | {"source_question_id": "revised", "core_task_complete": False}
    receipt = {
        "source_kind": "actual_human_reply",
        "reviewer_kind": "human",
        "cell_id": "cell",
        "source_packet": "packet",
        "source_packet_sha256": content_hash(packet),
        "observation_sha256": content_hash(observed),
        "blind_id": "blind",
        "initial_review_path": "initial",
        "initial_review_sha256": content_hash(initial),
        "adjudication_path": "revised",
        "adjudication_sha256": content_hash(revised),
        "source_question_ids": ["initial", "revised"],
        "complete_task_reading": True,
        "atomic_grades_complete": False,
        "core_task_complete": False,
    }
    values = {"packet": packet, "initial": initial, "revised": revised}
    monkeypatch.setattr("scripts.analyze_quality_confirmation.read", lambda p: values[p])
    result = bind_reading_human(receipt, {"cell": observed}, {"cell": packet})
    assert not result["atomic_grades_complete"] and "atoms" not in result
    assert result["task_judgment_question_ids"]["core_task_complete"] == "revised"
    with pytest.raises(ValueError, match="latest_source"):
        bind_reading_human(
            receipt | {"core_task_complete": True}, {"cell": observed}, {"cell": packet}
        )
    values["revised"] = revised | {"cell_id": "another"}
    with pytest.raises(ValueError, match="adjudication_changed"):
        bind_reading_human(receipt, {"cell": observed}, {"cell": packet})


def test_failure_review_never_treats_guarded_or_nonempty_output_as_an_empty_failure(monkeypatch):
    observed = {"state": "failed", "report_summary": {"proposal": None}}
    reading = {
        "items": [
            {"review_id": "F01", "cell_id": "cell", "observation_sha256": content_hash(observed)}
        ]
    }
    source = {
        "source_kind": "actual_human_reply",
        "reviewer_kind": "human",
        "reading_json": "reading",
        "reading_sha256": content_hash(reading),
        "source_question_id": "synthetic-test-only",
        "item_judgments": [
            {
                "review_id": "F01",
                "cell_id": "cell",
                "observation_sha256": content_hash(observed),
                "core_task_complete": False,
                "fact_atom_count": 0,
            }
        ],
    }
    monkeypatch.setattr("scripts.analyze_quality_confirmation.read", lambda _: reading)
    row = bind_failure_human(source, {"cell": observed})[0]
    assert not row["core_task_complete"] and row["fact_atom_count"] == 0
    with pytest.raises(ValueError, match="observation_binding_changed"):
        bind_failure_human(source, {"cell": observed | {"state": "guarded"}})


def test_core_omission_fails_full_completion_without_creating_false_fact_atoms():
    reading = {
        "cell_id": "cell",
        "source_question_id": "synthetic-test-only",
        "core_task_complete": False,
        "unambiguous_adjudicated_findings": [],
    }
    result = acceptance_from_witnesses(
        {"generated_core_event_cluster_mean_minimum": 1}, [], (), [reading]
    )
    assert result["status"] == "not_passed"
    assert [w["metric"] for w in result["verified_target_violations"]] == [
        "generated_core_event_cluster_mean_minimum"
    ]
    assert result["complete_population_semantic_rates"] is None


def test_complete_arm_metric_can_be_reported_without_imputing_other_arm():
    cells = {
        "a": {"cell_id": "a", "arm_id": "fixed", "event_cluster_id": "one"},
        "b": {"cell_id": "b", "arm_id": "agent", "event_cluster_id": "one"},
    }
    rows = [
        {
            "cell_id": "a",
            "source_question_id": "synthetic-test-only",
            "core_task_complete": True,
            "all_requirements_verdict": "satisfied_with_minor_scope_caveat",
        }
    ]
    result = generated_metric_coverage(cells, {"a": 1, "b": 8}, rows)
    assert result["fixed"]["core_task_complete"]["event_cluster_mean"] == 1
    assert result["fixed"]["unqualified_requirements"]["event_cluster_mean"] == 0
    assert result["agent"]["core_task_complete"]["event_cluster_mean"] is None
    assert result["agent"]["unqualified_requirements"]["actually_judged"] == 0


def test_human_index_requires_exact_source_content_and_experiment(tmp_path):
    source = {"synthetic_test_only": True}
    source_path = tmp_path / "source.json"
    write_new(source_path, source)
    index = {
        "experiment_id": "synthetic-test-only",
        "human": [],
        "human_stage": [],
        "human_reading": [{"path": source_path.as_posix(), "content_sha256": content_hash(source)}],
        "human_failure": [],
    }
    path = tmp_path / "index.json"
    write_new(path, index)
    assert load_human_index(path, "synthetic-test-only")[2] == [source_path]
    with pytest.raises(ValueError, match="another_experiment"):
        load_human_index(path, "other")
    source_path.write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="source_changed"):
        load_human_index(path, "synthetic-test-only")

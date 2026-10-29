"""Delivery closure cannot turn missing quality or unstarted cells into a passing experiment."""

from copy import deepcopy

import pytest

from ingestion.records import content_hash
from scripts.finalize_p116 import delivery_summary
from scripts.summarize_review_components import component_rows


def packet_fixture():
    packet = {
        "blind_id": "BLIND-SYNTHETIC",
        "proposal": {
            "claims": [{"claim_id": "C001", "support_ids": ["E001", "E002"], "counter_ids": []}]
        },
        "required_context_links": [{"claim_id": "C001", "context_aliases": ["E002"]}],
    }
    review = {
        "blind_id": packet["blind_id"],
        "claim_verdicts": {"C001": "supported"},
        "packet_content_sha256": content_hash(packet),
        "reviewed_claims": [
            {
                "claim_id": "C001",
                "direct_evidence": ["E001"],
                "required_context_only": ["E002"],
                "human_reason": "SYNTHETIC role fixture, not a human reply",
                "unsupported_evidence": [],
            }
        ],
    }
    return review, packet


def test_component_transcription_keeps_context_separate_from_semantic_support():
    review, packet = packet_fixture()
    rows = component_rows(review, packet)
    assert len(rows) == 2
    assert rows[0]["support_judgment"] == "supported"
    assert rows[1]["support_judgment"] is None
    assert rows[1]["also_required_context_by_program"] is True
    assert not any(r["whole_compound_claim_support_inferred"] for r in rows)


@pytest.mark.parametrize("mutation", ["missing_pair", "invented_alias", "unrequired_context"])
def test_incomplete_or_changed_component_roles_are_rejected(mutation):
    review, packet = packet_fixture()
    if mutation == "missing_pair":
        review["reviewed_claims"][0]["required_context_only"] = []
    elif mutation == "invented_alias":
        review["reviewed_claims"][0]["direct_evidence"] = ["E099"]
    else:
        packet["required_context_links"] = []
        review["packet_content_sha256"] = content_hash(packet)
    with pytest.raises(ValueError):
        component_rows(review, packet)


def delivery_fixture():
    methods = ("fixed", "single_agent", "parallel_tools", "multi_agent")
    raw = {
        "experiment": "SYNTHETIC",
        "rows": [
            {"state": state}
            for state, n in [
                ("completed", 85),
                ("guarded", 120),
                ("failed", 64),
                ("not_started", 1),
            ]
            for _ in range(n)
        ],
        "summary": {
            "arms": {
                m: {
                    "frozen_tasks_and_repeats": 60,
                    "states": {"completed": 27, "guarded": 30, "failed": 3},
                }
                for m in methods
            }
        },
        "challenge_results": [],
    }
    supplement = {
        "arms": {
            m: {"provider": {"requests_reserved": 0}, "mechanism_observations": {}} for m in methods
        }
    }
    dev = {
        "human_reviewed_cells": 8,
        "quality_baseline": {"reviewed_tasks": 8},
        "actual_new_provider_requests": 3,
    }
    components = {
        "reviewed_answer_tasks": 3,
        "citation_pairs_reviewed": 24,
        "direct_aspect_support_judgments": 23,
        "direct_aspect_supported": 23,
        "direct_aspect_support_fraction": 1.0,
    }
    return raw, supplement, dev, components, {"status": "draft"}


def test_delivery_status_preserves_unmeasured_quality_and_original_unstarted_cell():
    args = delivery_fixture()
    result = delivery_summary(*args)
    assert result["module_delivery_complete"] is True
    assert result["all_original_quality_requirements_passed"] is False
    assert result["formal_model_quality_acceptance"] == "not_established"
    assert result["strict_population_task_success"] is None
    assert result["new_preregistered_unseen_trial_executed"] is False
    assert result["original_states"]["not_started"] == 1
    assert result["new_provider_requests_for_delivery"] == 0
    changed = list(deepcopy(args))
    changed[0]["rows"][-1]["state"] = "completed"
    with pytest.raises(ValueError, match="denominator_or_states"):
        delivery_summary(*changed)

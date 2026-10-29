"""Quality denominators and provenance cannot be satisfied by model assertions alone."""

from copy import deepcopy

import pytest

from quality.runner import check_approval
from quality.scoring import RELATIONS, metrics, validate_judgment


def sample():
    payload = {
        "proposal": {
            "claims": [
                {
                    "claim_id": "C001",
                    "statement": "仅限部分车辆，风险可能增加。",
                    "support_ids": ["E001"],
                    "counter_ids": [],
                }
            ]
        },
        "program_context_pairs": [],
        "obligations": [{"obligation_id": "R01"}],
        "required_relation_types": [],
        "execution_state": "completed",
    }
    judge = {
        "schema_version": "model-assisted-judgment/0.1",
        "complete_reading": True,
        "disposition_correct": True,
        "core_complete": True,
        "all_requirements": "satisfied",
        "claims": [
            {
                "claim_id": "C001",
                "verdict": "supported",
                "scope_verdict": "correct",
                "atoms": [
                    {
                        "atom_id": "A01",
                        "quote": "仅限部分车辆，",
                        "verdict": "supported",
                        "support_ids": ["E001"],
                        "reason": "synthetic scope",
                    },
                    {
                        "atom_id": "A02",
                        "quote": "风险可能增加。",
                        "verdict": "supported",
                        "support_ids": ["E001"],
                        "reason": "synthetic consequence",
                    },
                ],
                "reason": "synthetic",
            }
        ],
        "citations": [
            {
                "claim_id": "C001",
                "evidence_alias": "E001",
                "role": "direct",
                "atom_ids": ["A01", "A02"],
                "aspect_verdict": "supported",
                "whole_claim_verdict": "supported",
                "reason": "synthetic",
            }
        ],
        "obligations": [
            {
                "obligation_id": "R01",
                "verdict": "satisfied",
                "claim_ids": ["C001"],
                "support_ids": ["E001"],
                "reason": "synthetic",
            }
        ],
        "relations": [
            {"relation_type": key, "verdict": "not_applicable", "reason": "synthetic"}
            for key in sorted(RELATIONS)
        ],
        "missing_or_counterevidence": [],
        "reason": "SYNTHETIC fixture, never a quality observation",
    }
    return payload, judge


def test_valid_model_result_is_explicitly_not_human():
    payload, judge = sample()
    value = metrics(payload, judge)
    assert value["provenance"] == "model_judgment_not_human"
    assert value["strict_success"] is True
    assert value["atomic_verdict_counts"] == {"supported": 2}


@pytest.mark.parametrize(
    "change,code",
    [
        ("atom_omission", "atomic_review_leaves_uncovered_text"),
        ("invented_context", "invented_context_role"),
        ("missing_citation", "incomplete_citation_denominator"),
        ("missing_obligation", "incomplete_obligation_denominator"),
        ("new_evidence", "atom_adds_unlisted_support"),
        ("invented_quote", "atom_quote_not_exact"),
    ],
)
def test_incomplete_or_misbound_reviews_are_rejected(change, code):
    payload, judge = sample()
    if change == "atom_omission":
        judge["claims"][0]["atoms"].pop()
    elif change == "invented_context":
        judge["citations"][0]["role"] = "both"
    elif change == "missing_citation":
        judge["citations"] = []
    elif change == "missing_obligation":
        judge["obligations"] = []
    elif change == "new_evidence":
        judge["claims"][0]["atoms"][0]["support_ids"] = ["E999"]
    else:
        judge["claims"][0]["atoms"][0]["quote"] = "所有车辆安全"
    with pytest.raises(ValueError, match=code):
        validate_judgment(payload, judge)


def test_failure_cannot_become_success_or_unknown_a_zero():
    payload, judge = sample()
    payload["execution_state"] = "failed"
    assert metrics(payload, judge)["strict_success"] is False
    unknown = deepcopy(judge)
    unknown["claims"][0]["atoms"][0]["verdict"] = "not_evaluable"
    assert metrics(payload, unknown)["strict_success"] is None


def test_required_relation_cannot_be_removed_to_raise_accuracy():
    payload, judge = sample()
    payload["required_relation_types"] = ["scope_overlap"]
    with pytest.raises(ValueError, match="required_relation_evaded"):
        validate_judgment(payload, judge)


def test_prior_generation_approval_cannot_authorize_new_reviewer():
    with pytest.raises(ValueError, match="no_matching_new_reviewer"):
        check_approval(
            "plan", [{"job_id": "new"}], {"maximum_requests": 584, "model": "deepseek-flash"}
        )

"""Synthetic memory contracts and policies, without provider calls or databases."""

import pytest
from pydantic import ValidationError

from memory.contracts import Proposal, Review
from memory.policies import verify
from memory.runtime import augment


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "user", "project_id": "recall"},
        {"scope": "project", "project_id": None},
        {"kind": "rule"},
        {"kind": "source_pointer"},
        {"policy_id": "keep_applicability_unknown"},
        {"kind": "preference", "release_id": "a" * 64},
        {"semantic_key": "../grant"},
        {"expires_at": "2026-01-01T00:00:00"},
        {"owner_id": "another-user"},
    ],
)
def test_memory_scope_authority_and_time_are_strict(change):
    base = {
        "kind": "preference",
        "scope": "project",
        "project_id": "recall",
        "semantic_key": "response.language",
        "value": "中文",
    }
    with pytest.raises(ValidationError):
        Proposal.model_validate(base | change)


def test_domain_pointer_never_accepts_an_official_fact_as_its_value():
    with pytest.raises(ValidationError):
        Proposal(
            kind="source_pointer",
            scope="project",
            project_id="recall",
            semantic_key="source.prius",
            release_id="a" * 64,
            sources=[{"source_id": "SYNTHETIC"}],
            value="车型相同所以安全",
        )


@pytest.mark.parametrize("policy", ["keep_applicability_unknown", "replay_original_before_claim"])
def test_installed_operating_rules_have_versioned_bounded_checks(policy):
    result = verify(policy)
    assert all(result["checks"].values()) and result["implementation_sha256"]


def test_unknown_rule_and_unversioned_review_are_rejected():
    with pytest.raises(ValueError):
        verify("trust_all_model_claims")
    with pytest.raises(ValidationError):
        Review(
            expected_revision=1,
            proposal_sha256="a" * 64,
            decision="approve",
            note="",
            approved=True,
        )


def test_memory_default_off_preserves_historical_prompt_exactly():
    payload = {"evidence": [], "question": "SYNTHETIC"}
    system, actual = augment("original", payload)
    assert system == "original" and actual is payload

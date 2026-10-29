"""A post hoc analysis revision, never a new pre-registered quality target."""

import hashlib
from pathlib import Path

POLICY = {
    "schema_version": "role-aware-policy/0.2",
    "timing": "post_hoc_after_test_outputs_and_one_actual_human_review",
    "generation_or_reference_changes": False,
    "scope": "same_rules_for_every_frozen_arm_no_selective_rescoring",
    "direct_metric": "supported_direct_pairs_divided_by_evaluated_direct_pairs_for_declared_aspects",
    "context_metric": "mechanical_required_context_links_present_divided_by_expected_links",
    "context_is_semantically_supported": False,
    "partial_assertions_separate_from_unsupported_assertions": True,
    "whole_claim_support_requires_separate_claim_judgment": True,
    "whole_claim_citation_support_rate": "not_inferred_from_aspect_judgments",
    "source_visibility": "exact_minimized_text_and_source_or_structured_payload; unresolved_is_not_absence",
    "provenance_boundary": "trusted reply registry checked by maintainer; schema flags alone do not authenticate humans",
    "review_conflicts": "pending_until_actual_adjudication_in_a_new_source_registry_revision",
    "task_success_requires_actual_task_judgment": True,
    "positive_task_success_requires_complete_positive_claim_citation_obligation_relation_reviews": True,
    "negative_task_success_does_not_impute_unreviewed_metric_components": True,
    "unreviewed_or_disputed_metrics": None,
    "aggregate_rate_requires_complete_frozen_denominator": True,
    "original_v0_1_outputs_scores_and_review_index": "preserved",
    "quality_thresholds": "not_set_post_hoc_requires_independent_development_baseline",
}


def implementation():
    return {
        p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path("reviewing").glob("*.py"))
    }

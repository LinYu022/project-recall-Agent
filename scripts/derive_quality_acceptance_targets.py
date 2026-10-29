"""Derive prospective point targets from actual completed human development review."""

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import mean

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    atomic = read("docs/verification/2026-10-29-P1-16-human-components-complete.json")
    old_targets = read("evals/development-baseline-v0.1/quality-targets-draft.json")
    old_review = read("docs/verification/2026-10-29-P1-16-development-review-complete.json")
    citation = read("docs/verification/2026-10-29-P1-16-component-review.json")
    if not atomic["complete_development_component_baseline"]:
        raise ValueError("incomplete_actual_human_component_baseline")
    clusters = {r["cell"]["case_id"]: r["cell"]["event_cluster_id"] for r in old_review["rows"]}
    counts = defaultdict(lambda: defaultdict(int))
    for row in atomic["rows"]:
        for key, value in row["atom_counts"].items():
            counts[clusters[row["case_id"]]][key] += value
    fractions = {
        group: {
            key: by.get(key, 0) / sum(by.values())
            for key in ("supported", "partial", "unsupported")
        }
        for group, by in counts.items()
    }
    targets = {
        "disposition_appropriateness_minimum": old_targets["targets"][
            "disposition_appropriateness"
        ]["development_point"],
        "generated_core_event_cluster_mean_minimum": old_targets["targets"][
            "generated_core_event_cluster_mean"
        ]["development_point"],
        "generated_unqualified_requirements_event_cluster_mean_minimum": old_targets["targets"][
            "generated_unqualified_requirements_event_cluster_mean"
        ]["development_point"],
        "stop_stage_appropriateness_minimum": old_targets["targets"][
            "appropriate_stop_or_limited_answer"
        ]["development_point"],
        "atomic_unsupported_event_cluster_mean_maximum": mean(
            v["unsupported"] for v in fractions.values()
        ),
        "atomic_partial_event_cluster_mean_maximum": mean(v["partial"] for v in fractions.values()),
        "named_aspect_citation_support_minimum": citation["direct_aspect_support_fraction"],
        "required_context_coverage_minimum": 1.0,
        "required_relation_type_handling_minimum": 1.0,
        "all_declared_hard_boundary_checks_must_pass": True,
    }
    value = {
        "schema_version": "prospective-human-derived-quality-targets/0.2",
        "status": "candidate_pending_reference_audit_and_full_protocol_freeze",
        "targets": targets,
        "atomic_cluster_baseline": fractions,
        "atomic_counts": atomic["atomic_counts"],
        "human_reviewed_answer_tasks": 3,
        "human_reviewed_stop_tasks": 5,
        "independent_reviewers": 1,
        "independent_seen_event_clusters": 2,
        "sources": {
            "atomic_components": content_hash(atomic),
            "earlier_core_and_stage_baseline": content_hash(old_review),
            "named_citation_aspects": content_hash(citation),
        },
        "citation_metric_scope": "Human named direct aspects, separate from whole compound claim support and atomic partialness. Required context is not direct support.",
        "cross_claim_context_policy": "Only explicitly reviewed prior-claim facts; do not add unlisted aliases to current report.",
        "relation_baseline": "One actual human-reviewed Ford pair: confirmed overlap/symptom correctly stated, unsupported candidate official-reference/exclusion correctly withheld, applicability unknown correctly retained.",
        "metric_complete_population_requires_all_frozen_units_evaluated": True,
        "failed_unstarted_or_missing_units": "Retain full denominator; completion false for failed/unstarted, semantic components remain missing if not observed. No success imputation.",
        "judge_calibration_status": "External DeepSeek reviewers failed calibration; their scores are provisional, require actual human adjudication for acceptance.",
        "confidence_interval": None,
        "limitation": "Development point non-regression targets, one human and two seen events; not a population guarantee. Prospective test may fail.",
        "old_270_cells_retroactively_accepted": False,
        "new_test_executed": False,
    }
    write_new(args.output, value)
    print(
        {"output": args.output.as_posix(), "targets": targets, "atomic_cluster_baseline": fractions}
    )


if __name__ == "__main__":
    main()

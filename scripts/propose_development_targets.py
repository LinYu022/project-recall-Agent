"""Development-derived numerical draft; no retroactive targets or fabricated atomic grades."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def build_targets(summary):
    reviews = summary["actual_human_review_records"]
    cells = {r["cell"]["case_id"]: r["cell"] for r in summary["rows"]}
    identifiers = [r["case_id"] for r in reviews]
    if len(set(identifiers)) != len(identifiers) or set(identifiers) != set(cells):
        raise ValueError("targets_require_every_development_case_once_no_posthoc_exclusion")
    answers = [r for r in reviews if r["review_scope"] == "generated_answer"]
    stops = [r for r in reviews if r["review_scope"] == "stop_or_limited_answer"]
    if not answers or not stops:
        raise ValueError("targets_require_both_answer_and_stop_strata")

    def cluster_mean(field):
        groups = defaultdict(list)
        for row in answers:
            if field == "core":
                if type(row["core_task_complete"]) is not bool:
                    raise ValueError("core_judgment_unreviewed")
                point = int(row["core_task_complete"])
            else:
                verdict = row["all_required_conditions_verdict"]
                if verdict not in {
                    "satisfied",
                    "mostly_satisfied_with_minor_caveat",
                    "not_satisfied",
                }:
                    raise ValueError("requirement_judgment_unreviewed")
                # This indicator counts only the unqualified category; original categories remain.
                point = int(verdict == "satisfied")
            groups[cells[row["case_id"]]["event_cluster_id"]].append(point)
        return sum(sum(v) / len(v) for v in groups.values()) / len(groups)

    if any(type(r["disposition_correct"]) is not bool for r in reviews) or any(
        type(r["stop_or_limited_answer_requirements_satisfied"]) is not bool for r in stops
    ):
        raise ValueError("disposition_or_stop_judgment_unreviewed")
    values = {
        "disposition_appropriateness": sum(r["disposition_correct"] for r in reviews)
        / len(reviews),
        "generated_core_event_cluster_mean": cluster_mean("core"),
        "generated_unqualified_requirements_event_cluster_mean": cluster_mean("unqualified"),
        "appropriate_stop_or_limited_answer": sum(
            r["stop_or_limited_answer_requirements_satisfied"] for r in stops
        )
        / len(stops),
    }
    return {
        "schema_version": "development-derived-target-draft/0.1",
        "status": "draft_for_future_unseen_protocol_not_applied_to_existing_runs",
        "basis": "no_regression_point_targets_from_this_fully_reviewed_seen_development_pilot",
        "review_summary_content_sha256": content_hash(summary),
        "plan_id": summary["plan_id"],
        "planned_answer_tasks": len(answers),
        "planned_stop_or_limited_answer_tasks": len(stops),
        "targets": {k: {"development_point": v, "proposed_minimum": v} for k, v in values.items()},
        "unqualified_indicator_is_not_a_binary_regrade_of_minor_caveats": True,
        "failed_unknown_and_missing_future_units": "retain_full_predeclared_denominator_no_exclusion",
        "same_targets_for_future_compared_methods": True,
        "future_test_protocol_id": None,
        "atomic_unsupported_rate_target": None,
        "uniform_citation_support_target": None,
        "strict_all_obligation_task_success_target": None,
        "unmeasured_metrics": "remain_pending_independent_component_annotation_never_inferred_from_core_pass",
        "confidence_interval": None,
        "original_frozen_test_reclassified_or_accepted": False,
        "ready_for_full_test_freeze": False,
        "limitations": "one reviewer, two seen events, one method and one repeat; no population guarantee or method benefit",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = read(args.review_summary)
    for item in summary["actual_human_review_records"]:
        if content_hash(read(item["path"])) != item["content_sha256"]:
            raise ValueError("human_source_changed_before_target_draft")
    value = build_targets(summary)
    write_new(args.output, value)
    print(json.dumps({"output": args.output.as_posix(), "status": value["status"]}))


if __name__ == "__main__":
    main()

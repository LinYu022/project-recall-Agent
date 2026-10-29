"""Seal human-audited targets/references and a human adjudication plan before new outputs."""

import argparse
import hashlib
from pathlib import Path
from random import Random

from evaluation.contracts import Protocol, cells, request_ceiling
from evaluation.freeze import seal
from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def wrapper_identity():
    names = [
        "scripts/freeze_quality_confirmation.py",
        "scripts/run_quality_confirmation.py",
        "scripts/revise_quality_references.py",
        "scripts/derive_quality_acceptance_targets.py",
    ]
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = read(args.candidate)
    target = read("evals/quality-acceptance-v0.1/prospective-targets-candidate.json")
    human = read("docs/experiments/P1-16-quality-human-reference-audit.json")
    if value["actual_human_reference_audit_sha256"] != content_hash(human):
        raise ValueError("reference_audit_changed")
    main_cases = [c["case_id"] for c in value["cases"]]
    profile = read("evals/development-baseline-v0.1/prepared-plan.json")["profile"]
    prefix = {
        "schema_version": "quality-confirmation-prefix/0.1",
        "history": [],
        "memory_enabled": False,
        "original_ablation_results_remain_separate": True,
    }
    methods = ["fixed", "single_agent", "parallel_tools", "multi_agent"]
    # Every default-method cell is a mandatory human review; other methods use fixed
    # cluster strata plus every hard-boundary alarm. No unreviewed model grade is certified.
    rng = Random(20261029)
    human_sample = []
    for method in methods:
        for group in value["groups"]:
            ids = [c["case_id"] for c in value["cases"] if c["event_cluster_id"] == group]
            selected = ids if method == "fixed" else rng.sample(sorted(ids), 3)
            human_sample += [f"{method}:{identifier}:1" for identifier in selected]
    protocol = Protocol.model_validate(
        {
            "dataset_release_id": value["release"]["release_id"],
            "manifest_sha256": value["release"]["manifest_sha256"],
            "cases": value["cases"],
            "arms": [
                {"arm_id": m, "family": "main", "method": m, "cases": main_cases} for m in methods
            ],
            "profile": profile,
            "previously_seen_campaigns": sorted(
                set(
                    read("evals/comprehensive-v0.1/protocol.json")["previously_seen_campaigns"]
                    + [
                        code
                        for group in read("evals/comprehensive-v0.1/protocol.json")[
                            "cluster_campaigns"
                        ].values()
                        for code in group
                    ]
                )
            ),
            "cluster_campaigns": value["groups"],
            "frozen_prefix_sha256": content_hash(prefix),
            "quality_targets": {
                "prospective_numerical": target["targets"],
                "human_development_targets_sha256": content_hash(target),
                "human_reference_audit_sha256": content_hash(human),
                "candidate_sha256": content_hash(value),
                "new_wrapper_implementation": wrapper_identity(),
                "stage_obligations": value["stage_obligations"],
                "reference_relations": value["reference_relations"],
                "mandatory_human_cells": sorted(human_sample),
                "model_judge_status": "failed_calibration_not_accepted_for_certification",
                "no_model_grade_promoted_to_human": True,
                "unreviewed_metric_components_remain_null": True,
                "full_population_semantic_rate_requires_complete_adjudication": True,
                "may_report_acceptance_failure_as_soon_as_a_verified_target_violation_is_established": True,
                "no_success_claim_without_all_required_measurements": True,
                "additional_human_selection": "all_critical_boundary_or_reference_ambiguity_flags_plus_predeclared_80_cells",
                "human_sample_role": "audit/stratified subset, not whole-population success denominator",
                "statistical_unit": "five_event_clusters; repetitions_do_not_add_independent_events",
                "atomic_partial_denominator": "human_adjudicated_whole_conditional_propositions_not_punctuation_fragments",
                "old_tests_retroactively_preregistered": False,
            },
        }
    )
    execution_order = []
    for case_id in main_cases:
        ordered = list(methods)
        rng.shuffle(ordered)
        execution_order += [f"{m}:{case_id}:1" for m in ordered]
    protocol.quality_targets["execution_order"] = execution_order
    for case_id, reference in value["references"].items():
        write_new(args.labels / "references" / (case_id + ".json"), reference)
    write_new(args.labels / "protocol.json", protocol.model_dump(mode="json"))
    write_new(args.labels / "prefix.json", prefix)
    identity = seal(protocol, args.output, prefix)
    write_new(args.output / "active-confirmation.json", identity)
    write_new(
        args.labels / "release-location.json",
        {"root": value["release_root"], "candidate": args.candidate.as_posix()},
    )
    write_new(
        args.labels / "call-proposal.json",
        {
            "experiment_id": identity["experiment_id"],
            "endpoint": protocol.endpoint,
            "model": protocol.model_id,
            "maximum_requests": request_ceiling(protocol),
            "cells": len(cells(protocol)),
            "per_cell_completion_tokens": 4000,
            "per_cell_prompt_characters": 96000,
            "provenance": "human_audited_sources_targets_and_sampling_before_any_new_model_output",
            "source_ids": sorted({s for c in protocol.cases for s in c.sources}),
            "maximum_input_character_budget": 60 * 96000,
            "maximum_completion_token_budget": 60 * 4000,
            "human_sample_cells": sorted(human_sample),
            "approved": False,
        },
    )
    print(
        {
            "experiment_id": identity["experiment_id"],
            "cells": len(cells(protocol)),
            "maximum_requests": request_ceiling(protocol),
            "mandatory_human_cells": len(human_sample),
            "generation_calls": 0,
        }
    )


if __name__ == "__main__":
    main()

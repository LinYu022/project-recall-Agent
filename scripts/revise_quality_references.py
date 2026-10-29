"""Apply actual human label corrections before freeze; keep them away from generation."""

import argparse
from copy import deepcopy
from pathlib import Path

from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from reviewing.corpus import read, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = read(args.candidate)
    value = deepcopy(original)
    audit = read("docs/experiments/P1-16-quality-human-reference-audit.json")
    stage = {}
    for case in value["cases"]:
        identifier = case["case_id"]
        ref = value["references"][identifier]
        for obligation in ref["required_claims"]:
            name = obligation["reference_claim_id"]
            parts = name.split("-")
            if len(parts) == 3 and parts[1] in audit["source_obligations"]:
                mandatory = audit["source_obligations"][parts[1]][parts[2].lower()]
                obligation["statement"] += (
                    "\nHuman-audited mandatory aspects (within relevant query scope): "
                    + "; ".join(mandatory)
                )
        if identifier == audit["BMW_year_revision"]["case_id"]:
            case["scope"]["model_year"] = 2022
        if identifier.endswith("CONDITIONS"):
            stage[identifier] = [
                {
                    "obligation_id": "R-CLARIFY-INDIVIDUAL-CONDITIONS",
                    "statement": audit["global_stage_requirements"]["CONDITIONS"],
                    "basis": "actual_guard_and_explicit_boundary_not_a_fabricated_evidence_card",
                }
            ]
        elif identifier.endswith("YEAR"):
            stage[identifier] = [
                {
                    "obligation_id": "R-BOUNDED-ZERO-MATCH",
                    "statement": audit["global_stage_requirements"]["YEAR"],
                    "basis": "actual_lookup_and_coverage",
                }
            ]
        elif identifier.endswith("TIME"):
            stage[identifier] = [
                {
                    "obligation_id": "R-TIME-SCOPE-CLARIFICATION",
                    "statement": audit["global_stage_requirements"]["TIME"],
                    "basis": "actual_guard_no_automatic_acceptance",
                }
            ]
        elif identifier.endswith("INSUFFICIENT"):
            stage[identifier] = [
                {
                    "obligation_id": "R-NO-COMPLAINT-EXPOSURE-DATA",
                    "statement": audit["global_stage_requirements"]["INSUFFICIENT"],
                    "basis": "actual_disposition_and_source_coverage",
                }
            ]
        elif identifier == "QTEST-24V744000-PAIR":
            stage[identifier] = [
                {
                    "obligation_id": audit["Honda_missing_investigation_obligation"]["id"],
                    "statement": audit["Honda_missing_investigation_obligation"]["statement"],
                    "basis": "actual_limited_source_answer",
                }
            ]
        checked = ReferenceCase.model_validate(ref).model_dump(mode="json")
        value["references"][identifier] = checked
        case["reference_sha256"] = content_hash(checked)
    value |= {
        "schema_version": "quality-confirmation-candidate/0.2",
        "previous_candidate_sha256": content_hash(original),
        "actual_human_reference_audit_sha256": content_hash(audit),
        "stage_obligations": stage,
        "reference_relations": audit["reference_relations"],
        "scoring_only_labels_never_sent_to_generator": True,
        "runtime_relationship_facts_added_from_test_labels": False,
        "new_test_outputs_used_for_tuning": False,
        "status": "human_audited_candidate_pending_protocol_freeze",
    }
    write_new(args.output, value)
    print(
        {
            "output": args.output.as_posix(),
            "stage_contracts": len(stage),
            "BMW_year": 2022,
            "generator_calls": 0,
        }
    )


if __name__ == "__main__":
    main()

"""Compare actually returned calibration judgments with existing human records."""

import argparse
from collections import Counter
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ledger = read(args.root / "requests.json")
    provider = ArtifactStore(args.root / "provider")
    expected = {}
    summary = read("docs/verification/2026-10-29-P1-16-development-review-complete.json")
    for row in summary["actual_human_review_records"]:
        human = read(row["path"])
        expected["DEV-" + human["blind_id"]] = human
    human = read("docs/experiments/P1-16-human-review-complete.json")
    expected["OLD-" + human["blind_id"]] = human
    rows = []
    claim_total = claim_agree = 0
    for job_id in ledger["spec"]["jobs"]:
        paths = [
            args.root / "results" / (job_id + "-repair1.json"),
            args.root / "results" / (job_id + ".json"),
        ]
        result = read(next(p for p in paths if p.exists()))
        raw = provider.read_json(result["provider_artifact"]["sha256"])
        judgment = (raw["response"] or {}).get("proposal") or {}
        human = expected[job_id]
        differences = []
        for item in judgment.get("claims", []):
            actual = human.get("claim_verdicts", {}).get(item["claim_id"])
            if actual is not None:
                claim_total += 1
                claim_agree += actual == item["verdict"]
                if actual != item["verdict"]:
                    differences.append(
                        {"claim_id": item["claim_id"], "human": actual, "model": item["verdict"]}
                    )
        rows.append(
            {
                "job_id": job_id,
                "mechanical_error": result["error"],
                "metrics": result["metrics"],
                "human_source_sha256": content_hash(human),
                "claim_disagreements": differences,
                "human_all_requirements": human.get("all_required_conditions_verdict"),
                "model_all_requirements": judgment.get("all_requirements"),
                "atomic_granularity_lengths": [
                    [len(a["quote"]) for a in c["atoms"]] for c in judgment.get("claims", [])
                ],
            }
        )
    usage = {
        key: sum((e.get("usage") or {}).get(key, 0) for e in ledger["entries"])
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    value = {
        "schema_version": "quality-calibration-assessment/0.1",
        "status": "failed_not_authorized_for_population_acceptance",
        "requests": len(ledger["entries"]),
        "approved_maximum": ledger["spec"]["maximum_requests"],
        "unused_requests_not_repurposed": ledger["spec"]["maximum_requests"]
        - len(ledger["entries"]),
        "ledger_sha256": content_hash(ledger),
        "usage": usage,
        "actual_account_charge": None,
        "calibration_rows": rows,
        "schema_valid_jobs": sum(r["mechanical_error"] is None for r in rows),
        "fully_evaluated_jobs": sum(
            bool((r["metrics"] or {}).get("complete_components")) for r in rows
        ),
        "human_claim_judgments": claim_total,
        "model_human_claim_agreements": claim_agree,
        "agreement_descriptive_only": claim_agree / claim_total if claim_total else None,
        "negative_control_partial_claims_detected": sum(
            d["model"] == "partial"
            for r in rows
            for d in r["claim_disagreements"]
            if d["human"] == "partial"
        ),
        "requests_states": dict(Counter(e["state"] for e in ledger["entries"])),
        "failure_reasons": [
            "Historical negative claims were overaccepted, despite known independent human partial judgments.",
            "Seen-development certain qualifier caveat was omitted by the judge.",
            "Several stop-stage judgments remain not_evaluable despite explicit stage requirements.",
            "One multi-proposition answer was segmented as one atom per long claim; this cannot establish atomic rates.",
            "Historical negative sample remains mechanically invalid after its one permitted repair.",
        ],
        "population_reviews_executed": 0,
        "new_confirmatory_generation_requests": 0,
        "formal_model_quality_acceptance": "not_established",
        "old_outputs_and_human_judgments_changed": False,
    }
    write_new(args.output, value)
    print(
        {
            "output": args.output.as_posix(),
            "requests": value["requests"],
            "schema_valid_jobs": value["schema_valid_jobs"],
            "complete_jobs": value["fully_evaluated_jobs"],
            "claim_agreement": [claim_agree, claim_total],
            "usage": usage,
        }
    )


if __name__ == "__main__":
    main()

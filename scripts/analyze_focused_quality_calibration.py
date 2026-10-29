"""Preserve a failed focused judge calibration; do not repurpose unspent approvals."""

import argparse
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
    store = ArtifactStore(args.root / "provider")
    results = []
    for job in ("FQC-QUALIFIER", "FQC-BOUNDARY", "FQC-STAGES"):
        value = read(args.root / (job + ".json"))
        raw = store.read_json(value["artifact"]["sha256"])
        results.append(
            value
            | {"observed_model_judgment_even_if_invalid": (raw["response"] or {}).get("proposal")}
        )
    usage = {
        k: sum((e.get("usage") or {}).get(k, 0) for e in ledger["entries"])
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    value = {
        "schema_version": "focused-quality-calibration-results/0.1",
        "plan_id": ledger["plan_id"],
        "status": "failed_not_accepted_as_reliable_population_judge",
        "actual_provider_requests": len(ledger["entries"]),
        "approved_maximum": 6,
        "unused_not_repurposed": 6 - len(ledger["entries"]),
        "ledger_sha256": content_hash(ledger),
        "usage": usage,
        "actual_account_charge": None,
        "results": results,
        "limitations": [
            "Qualifier control again incorrectly judged the omitted certain qualifier as preserved.",
            "Boundary control used uncited evidence and non-exact or paraphrased source quotations; validator rejected it.",
            "Four stop-stage judgments agreed with existing human handling judgments, but do not establish full individual or historical investigation.",
            "Targeted development calibration cannot establish unseen general reviewer accuracy.",
        ],
        "human_followup_choice": "expand_actual_human_review_keep_model_outputs_as_provisional_hints",
        "population_scoring_executed": False,
        "new_confirmatory_generation_executed": False,
        "formal_model_quality_acceptance": "not_established",
    }
    write_new(args.output, value)
    print(
        {
            "output": args.output.as_posix(),
            "requests": len(ledger["entries"]),
            "usage": usage,
            "calibration": "failed",
        }
    )


if __name__ == "__main__":
    main()

"""Transcribe explicit human citation roles for reviewed samples; no atomic regrading."""

import argparse
import json
from collections import Counter
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def component_rows(review, packet):
    claims = {c["claim_id"]: c for c in packet["proposal"]["claims"]}
    if review["blind_id"] != packet["blind_id"] or set(review["claim_verdicts"]) != set(claims):
        raise ValueError("component_review_claim_or_packet_identity_changed")
    if content_hash(packet) != review["packet_content_sha256"]:
        raise ValueError("component_packet_hash_changed")
    expected = {
        (c["claim_id"], a) for c in claims.values() for a in c["support_ids"] + c["counter_ids"]
    }
    required = {
        (link["claim_id"], alias)
        for link in packet.get("required_context_links", [])
        for alias in link["context_aliases"]
    }
    result = []
    for item in review["reviewed_claims"]:
        identifier = item["claim_id"]
        direct, context = set(item["direct_evidence"]), set(item["required_context_only"])
        if direct & context:
            raise ValueError("direct_and_context_only_roles_conflict")
        if item.get("unsupported_evidence"):
            raise ValueError("unsupported_pair_requires_explicit_separate_support_judgment")
        for alias in sorted(direct | context):
            pair = (identifier, alias)
            if pair not in expected or (alias in context and pair not in required):
                raise ValueError("component_uses_unlisted_or_unrequired_context")
            result.append(
                {
                    "claim_id": identifier,
                    "evidence_alias": alias,
                    "human_role": "direct_support" if alias in direct else "required_context_only",
                    "also_required_context_by_program": pair in required,
                    "support_judgment": "supported" if alias in direct else None,
                    "judgment_basis": "explicit human citation support statement, retained in direct_evidence transcription",
                    "evaluated_aspect": (item.get("citation_aspects") or {}).get(alias)
                    or item.get("human_reason")
                    or item.get("human_reason_verbatim"),
                    "whole_compound_claim_support_inferred": False,
                }
            )
    if {(r["claim_id"], r["evidence_alias"]) for r in result} != expected:
        raise ValueError("component_citation_pairs_not_fully_reviewed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = read("docs/verification/2026-10-29-P1-16-development-review-complete.json")
    outputs, direct = [], 0
    for source in summary["actual_human_review_records"]:
        if source["review_scope"] != "generated_answer":
            continue
        review = read(source["path"])
        if content_hash(review) != source["content_sha256"]:
            raise ValueError("actual_human_source_changed")
        row = next(r for r in summary["rows"] if r["cell"]["case_id"] == source["case_id"])
        packet = read(row["review_packet_path"])
        pairs = component_rows(review, packet)
        n = sum(p["human_role"] == "direct_support" for p in pairs)
        direct += n
        outputs.append(
            {
                "case_id": source["case_id"],
                "actual_human_source": source,
                "pairs": pairs,
                "direct_support_judgments": n,
                "context_only_pairs": len(pairs) - n,
                "claim_verdicts": review["claim_verdicts"],
                "scope_verdict_counts": dict(
                    Counter(c["scope_verdict"] for c in review["reviewed_claims"])
                ),
                "required_context_links": packet.get("required_context_links", []),
                "cross_claim_context_notes": [
                    c["cross_claim_context"]
                    for c in review["reviewed_claims"]
                    if c.get("cross_claim_context")
                ],
                "atomic_assertion_rate": None,
            }
        )
    write_new(
        args.output,
        {
            "schema_version": "reviewed-development-component-transcription/0.1",
            "classification": "post_hoc_transcription_of_actual_human_named_aspects_not_new_grading",
            "source_summary_sha256": content_hash(summary),
            "reviewed_answer_tasks": len(outputs),
            "citation_pairs_reviewed": sum(len(o["pairs"]) for o in outputs),
            "direct_aspect_support_judgments": direct,
            "direct_aspect_supported": direct,
            "direct_aspect_support_fraction": 1.0 if direct else None,
            "whole_claim_citation_support_rate": None,
            "atomic_unsupported_rate": None,
            "population_quality": None,
            "rows": outputs,
            "limits": "Three seen-development answers, one human, two event clusters; certain omission preserved. No inference to the 270-cell test population.",
            "new_provider_requests": 0,
        },
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "reviewed_pairs": sum(len(o["pairs"]) for o in outputs),
            }
        )
    )


if __name__ == "__main__":
    main()

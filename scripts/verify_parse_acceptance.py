"""Replay all pinned source rows and source-reviewed PDF conditions; no network or model."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ingestion.records import prepare_import
from parsing.audit import AuditManifest, audit_sources
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from parsing.chunks import checked_chunks, expand_chunk
from parsing.citations import checked_parse, make_citation, resolve_citation
from parsing.contracts import ParseRequest
from parsing.parser import text_hash
from runtime.artifacts import ArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--parsed-root", type=Path, default=Path(".local/parsed-sources"))
    parser.add_argument("--chunk-root", type=Path, default=Path(".local/chunk-sets"))
    parser.add_argument("--audit-root", type=Path, default=Path(".local/parse-audits"))
    parser.add_argument("--pdf-snapshot", default="9d2d065bf1f844e7ab4ea63494a4b2f9")
    parser.add_argument("--recall-snapshot", default="61cd818ad3ae45c7bbdc94ecf0b479bc")
    parser.add_argument("--complaints-snapshot", default="d0be027566e94ad2aa0737d166df64b1")
    parser.add_argument(
        "--reference", type=Path, default=Path("evals/parse-acceptance-reference-v0.1.json")
    )
    parser.add_argument("--output", type=Path, default=Path(".local/parse-acceptance-real.json"))
    args = parser.parse_args()
    requests = []
    for snapshot_id in (args.pdf_snapshot, args.recall_snapshot, args.complaints_snapshot):
        batch = prepare_import(args.raw_root, snapshot_id)
        assert batch.rows and all(row.version is not None for row in batch.rows)
        requests.extend(
            ParseRequest(snapshot_id=snapshot_id, ordinal=row.ordinal) for row in batch.rows
        )
    manifest = AuditManifest(requests=requests)
    configs = [load_chunk_config(s) for s in ("structure", "fixed")]
    rules = load_structure_rules()
    ledger = audit_sources(
        args.raw_root, args.parsed_root, args.chunk_root, manifest, configs, rules
    )
    audit_ref = ArtifactStore(args.audit_root).put_json(ledger)
    assert ArtifactStore(args.audit_root).read_json(audit_ref["sha256"]) == ledger
    assert not ledger["failures"], "real source audit has blocking failures; inspect the ledger"
    reference = json.loads(args.reference.read_bytes())
    pdf = next(i for i in ledger["items"] if i["source"]["source_kind"] == "investigation_pdf")
    digest = pdf["parse_artifact"]["sha256"]
    assert digest == reference["parse_artifact_sha256"], (
        "source changed; review a new reference version"
    )
    parsed = checked_parse(args.raw_root, args.parsed_root, digest)
    assert len(parsed["units"]) == 1
    unit = parsed["units"][0]
    checks = []
    root_only = []
    for attempt in pdf["chunk_attempts"]:
        chunks = checked_chunks(
            args.raw_root, args.parsed_root, args.chunk_root, attempt["chunk_artifact"]["sha256"]
        )
        for condition in reference["conditions"]:
            quote = condition["quote"]
            assert unit["text"].count(quote) == 1
            start = unit["text"].index(quote)
            end = start + len(quote)
            leaf = next(
                n
                for n in chunks["chunks"]
                if n["searchable"]
                and any(
                    r["unit_id"] == unit["unit_id"] and r["start"] < end and start < r["end"]
                    for r in n["ranges"]
                )
            )
            bundle = expand_chunk(chunks, leaf["chunk_id"])
            assert any(quote in n["text"] for n in [bundle["focus"], *bundle["context"]])
            citation = make_citation(args.raw_root, args.parsed_root, digest, 0, start, end)
            result = resolve_citation(args.raw_root, args.parsed_root, citation)
            assert result["status"] == "verified" and result["page_number"] == 1
            checks.append(
                {
                    "id": condition["id"],
                    "strategy": attempt["strategy"],
                    "focus_chunk_id": leaf["chunk_id"],
                    "exact_citation": citation.model_dump(mode="json"),
                    "expanded_context_contains_exact_quote": True,
                    "reference_answer": condition["reference_answer"],
                }
            )
        for gap in attempt["coverage"]["root_only_ranges"]:
            root_only.append(
                {
                    "strategy": attempt["strategy"],
                    **gap,
                    "text_sha256": text_hash(unit["text"][gap["start"] : gap["end"]]),
                }
            )
    expected_gaps = [
        {k: v for k, v in gap.items() if k not in {"decision", "reason"}}
        for gap in reference["root_only_reviews"]
    ]
    assert root_only == expected_gaps, "unexpected root-only text requires manual review"
    assert len(ledger["warnings"]) == len(root_only), "unexpected warning requires review"
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "saved_source_replay_with_implementation_agent_reviewed_reference",
        "audit_artifact": audit_ref,
        "counts": ledger["counts"],
        "failures": ledger["failures"],
        "warnings": ledger["warnings"],
        "condition_checks": checks,
        "root_only_reviews": reference["root_only_reviews"],
        "reference_sha256": text_hash(args.reference.read_text(encoding="utf-8")),
        "inspection_basis": "original_full_page_image_review_reused_no_pdf_changes",
        "reviewed_by": "implementation_agent_not_independent_human_gold_labels",
        "synthetic_negation_checks": "see tests/test_parse_audit.py and offline verification",
        "real_explicit_negation_conclusion_available": False,
        "semantic_support_automatically_evaluated": False,
        "new_external_source_requests": 0,
        "external_model_calls": 0,
        "ocr_performed": False,
        "dataset_release": None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "counts": ledger["counts"],
                "condition_checks": len(checks),
                "output": args.output.as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

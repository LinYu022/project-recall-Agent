"""Replay the saved real PDF and complaint fields; no downloads or model calls."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ingestion.records import prepare_import
from parsing.citations import make_citation, resolve_citation
from parsing.contracts import ParseRequest
from parsing.parser import parse_source, text_hash
from runtime.artifacts import ArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--parsed-root", type=Path, default=Path(".local/parsed-sources"))
    parser.add_argument("--pdf-snapshot", default="9d2d065bf1f844e7ab4ea63494a4b2f9")
    parser.add_argument("--complaints-snapshot", default="d0be027566e94ad2aa0737d166df64b1")
    parser.add_argument("--output", type=Path, default=Path(".local/parser-real-checks.json"))
    args = parser.parse_args()
    store = ArtifactStore(args.parsed_root)
    doc = parse_source(args.raw_root, ParseRequest(snapshot_id=args.pdf_snapshot))
    assert doc["status"] == "parsed" and doc["total_pdf_pages"] == 1
    assert len(doc["units"]) == 1
    ref = store.put_json(doc)
    assert store.put_json(doc) == ref
    text = doc["units"][0]["text"]
    for heading in (
        "MANUFACTURER & PRODUCT INFORMATION",
        "FAILURE REPORT SUMMARY",
        "ACTION / SUMMARY INFORMATION",
    ):
        assert heading in text
    assert "ODI" in text and "Manufacturer" in text and "Total" in text
    checks = []
    for phrase in ("10V-039", "ODI will continue to monitor"):
        assert text.count(phrase) == 1
        start = text.index(phrase)
        citation = make_citation(
            args.raw_root, args.parsed_root, ref["sha256"], 0, start, start + len(phrase)
        )
        result = resolve_citation(args.raw_root, args.parsed_root, citation)
        assert result["status"] == "verified" and result["page_number"] == 1
        checks.append(
            {
                "quote": phrase,
                "start": start,
                "end": citation.end,
                "quote_sha256": citation.quote_sha256,
                "replayed": True,
            }
        )
        if phrase == "10V-039":
            directory = args.parsed_root / "reference-citations"
            directory.mkdir(exist_ok=True)
            path = directory / (ref["sha256"] + ".json")
            encoded = citation.model_dump_json(indent=2) + "\n"
            if path.exists():
                assert path.read_text(encoding="utf-8") == encoded
            else:
                path.write_text(encoded, encoding="utf-8")
    batch = prepare_import(args.raw_root, args.complaints_snapshot)
    preserved = 0
    for row in batch.rows:
        assert row.version is not None
        parsed = parse_source(
            args.raw_root, ParseRequest(snapshot_id=args.complaints_snapshot, ordinal=row.ordinal)
        )
        original = row.version["payload"]["summary"]
        assert parsed["status"] == "parsed" and len(parsed["units"]) == 1
        unit = parsed["units"][0]
        assert unit["text"] == original and unit["text_sha256"] == text_hash(original)
        assert len(unit["regions"]) == 1
        region = unit["regions"][0]
        assert original[region["start"] : region["end"]] == original
        store.put_json(parsed)
        preserved += 1
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_saved_official_pdf_extraction_and_json_preservation",
        "pdf_snapshot_id": args.pdf_snapshot,
        "pdf_raw_sha256": doc["source"]["raw_sha256"],
        "source_version_id": doc["source"]["source_version_id"],
        "parse_artifact": ref,
        "parser": doc["parser"],
        "parser_sha256": doc["parser_sha256"],
        "pdf_pages": 1,
        "pdf_text_characters": len(text),
        "layout_region_candidates": len(doc["units"][0]["regions"]),
        "citations": checks,
        "issues": doc["issues"],
        "ocr_performed": False,
        "complaints_snapshot_id": args.complaints_snapshot,
        "whole_complaint_fields_preserved": preserved,
        "complaint_statement_truth_adjudicated": False,
        "semantic_chunking_complete": False,
        "table_cell_semantics_verified_by_parser": False,
        "new_external_requests": 0,
        "external_model_calls": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "pdf_pages": 1,
                "citations": len(checks),
                "complaint_fields": preserved,
                "output": args.output.as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

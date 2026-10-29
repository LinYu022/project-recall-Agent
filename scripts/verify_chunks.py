"""Verify real saved chunk boundaries/cells and a source-reviewed scope-loss example."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from parsing.chunk_review import render_review
from parsing.chunks import build_chunks, checked_chunks, cite_chunk, expand_chunk
from parsing.citations import resolve_citation
from parsing.contracts import Citation, ParseRequest
from parsing.parser import parse_source
from runtime.artifacts import ArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--parsed-root", type=Path, default=Path(".local/parsed-sources"))
    parser.add_argument("--chunk-root", type=Path, default=Path(".local/chunk-sets"))
    parser.add_argument(
        "--parse-digest", default="0fe534557720cdae5ad25ae52cae06d4df58d9bdb406765c92bec580c1278a42"
    )
    parser.add_argument("--complaints-snapshot", default="d0be027566e94ad2aa0737d166df64b1")
    parser.add_argument("--output", type=Path, default=Path(".local/chunk-real-checks.json"))
    parser.add_argument("--review", type=Path, help="optional new inspection HTML output")
    args = parser.parse_args()
    rules = load_structure_rules()
    store = ArtifactStore(args.chunk_root)
    results, refs = {}, {}
    quote_replays = 0
    for strategy in ("structure", "fixed"):
        result = build_chunks(
            args.raw_root, args.parsed_root, args.parse_digest, load_chunk_config(strategy), rules
        )
        ref = store.put_json(result)
        assert store.put_json(result) == ref
        assert (
            checked_chunks(args.raw_root, args.parsed_root, args.chunk_root, ref["sha256"])
            == result
        )
        for chunk in result["chunks"]:
            if chunk["searchable"]:
                for citation in cite_chunk(
                    args.raw_root, args.parsed_root, result, chunk["chunk_id"]
                ):
                    replayed = resolve_citation(
                        args.raw_root, args.parsed_root, Citation.model_validate(citation)
                    )
                    assert replayed["status"] == "verified"
                    quote_replays += 1
        results[strategy], refs[strategy] = result, ref
    structured, fixed = results["structure"], results["fixed"]
    assert structured["parse_artifact_sha256"] == fixed["parse_artifact_sha256"]
    leaves = [n for n in structured["chunks"] if n["searchable"]]
    tables = [n for n in leaves if n["table"]]
    assert len(tables) == 1
    table = tables[0]["table"]
    assert table["columns"] == ["ODI", "Manufacturer", "Total"]
    assert table["rows"][0]["label"] == "Complaints:"
    assert table["rows"][0]["values"] == [1235, 192, 1427]
    assert (
        len(table["cells"]) == 18 and table["footnote"] == "*Description of Other: Warranty claims"
    )
    # Cell data and labels are checked against raw source locations, not generated table HTML.
    parsed = ArtifactStore(args.parsed_root).read_json(args.parse_digest)
    unit_text = {u["unit_id"]: u["text"] for u in parsed["units"]}
    for cell in table["cells"]:
        assert unit_text[cell["unit_id"]][cell["start"] : cell["end"]] == cell["raw"]
    scope = "Model Year 2010 Toyota Prius"
    focus = next(n for n in leaves if "The subject vehicles" in n["text"])
    assert scope not in focus["text"]
    expanded = expand_chunk(structured, focus["chunk_id"])
    assert any(scope in n["text"] for n in expanded["context"])
    fixed_focus = next(
        n for n in fixed["chunks"] if n["searchable"] and "The subject vehicles" in n["text"]
    )
    assert scope not in fixed_focus["text"]
    assert any(scope in n["text"] for n in expand_chunk(fixed, fixed_focus["chunk_id"])["context"])
    from ingestion.records import prepare_import

    batch = prepare_import(args.raw_root, args.complaints_snapshot)
    whole_by_strategy = {"structure": 0, "fixed": 0}
    longer_by_strategy = {"structure": 0, "fixed": 0}
    for row in batch.rows:
        assert row.version
        doc = parse_source(
            args.raw_root, ParseRequest(snapshot_id=args.complaints_snapshot, ordinal=row.ordinal)
        )
        digest = ArtifactStore(args.parsed_root).put_json(doc)["sha256"]
        for strategy in whole_by_strategy:
            config = load_chunk_config(strategy)
            result = build_chunks(args.raw_root, args.parsed_root, digest, config, rules)
            nodes = [n for n in result["chunks"] if n["searchable"]]
            if len(row.version["payload"]["summary"]) <= config.short_complaint_characters:
                assert len(nodes) == 1 and nodes[0]["text"] == row.version["payload"]["summary"]
                whole_by_strategy[strategy] += 1
            else:
                longer_by_strategy[strategy] += 1
            store.put_json(result)
    if args.review:
        render_review(structured, fixed, args.review)
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "saved_official_source_chunks_and_agent_reviewed_counterexample",
        "source": structured["source"],
        "parse_artifact_sha256": args.parse_digest,
        "chunk_artifacts": refs,
        "searchable_chunks": {
            s: sum(n["searchable"] for n in r["chunks"]) for s, r in results.items()
        },
        "configs": {s: load_chunk_config(s).model_dump(mode="json") for s in results},
        "replayed_leaf_citations": quote_replays,
        "table_cells_replayed": len(table["cells"]),
        "table_header": table["columns"],
        "table_first_row": table["rows"][0],
        "table_footnote": table["footnote"],
        "scope_loss_example": {
            "focus_chunk_id": focus["chunk_id"],
            "source_scope": scope,
            "leaf_alone_omits_scope": True,
            "expanded_context_contains_scope": True,
            "fixed_strategy_has_same_context_access": True,
            "forbidden_example": "本调查已确认所有混合动力车型都有该问题",
            "reference_answer": "subject vehicles 的产品范围是前文的 2010 Toyota Prius；不能扩大成所有混动车型。",
            "review": "implementation_agent_source_review_not_independent_human_labels",
        },
        "complaints_snapshot": args.complaints_snapshot,
        "short_fields_kept_whole": whole_by_strategy,
        "longer_fields": longer_by_strategy,
        "local_review_html": args.review.as_posix() if args.review else None,
        "retrieval_effectiveness_measured": False,
        "external_model_calls": 0,
        "new_external_source_requests": 0,
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
                "chunks": report["searchable_chunks"],
                "table_cells": 18,
                "quote_replays": quote_replays,
                "whole_complaints": whole_by_strategy,
                "output": args.output.as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

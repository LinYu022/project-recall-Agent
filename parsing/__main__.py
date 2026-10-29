"""Parse one pinned source row or create/replay an exact text citation."""

import argparse
import json
from pathlib import Path

from parsing.audit import AuditManifest, audit_sources
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from parsing.chunk_review import render_review
from parsing.chunks import build_chunks, checked_chunks, cite_chunk, expand_chunk
from parsing.citations import checked_parse, make_citation, resolve_citation
from parsing.contracts import Citation, ParseRequest
from parsing.parser import parse_source
from runtime.artifacts import ArtifactStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["parse", "cite", "verify", "chunk", "chunk-cite", "review-chunks", "audit"],
    )
    parser.add_argument(
        "identifier", help="snapshot ID, parse artifact digest, or citation JSON file"
    )
    parser.add_argument("--raw-root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--parsed-root", type=Path, default=Path(".local/parsed-sources"))
    parser.add_argument("--ordinal", type=int, default=0)
    parser.add_argument("--unit", type=int, default=0)
    parser.add_argument("--find", help="exact unique text to cite in one page/field")
    parser.add_argument("--output", type=Path, help="save a new citation file without overwriting")
    parser.add_argument("--chunk-root", type=Path, default=Path(".local/chunk-sets"))
    parser.add_argument("--audit-root", type=Path, default=Path(".local/parse-audits"))
    parser.add_argument("--strategy", choices=["structure", "fixed"], default="structure")
    parser.add_argument("--chunk-id", help="chunk ID within a saved chunk set")
    parser.add_argument("--compare", help="fixed chunk-set digest for review-chunks")
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            report = audit_sources(
                args.raw_root,
                args.parsed_root,
                args.chunk_root,
                AuditManifest.model_validate_json(Path(args.identifier).read_bytes()),
                [load_chunk_config(s) for s in ("structure", "fixed")],
                load_structure_rules(),
            )
            ref = ArtifactStore(args.audit_root).put_json(report)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "quality": report["quality"],
                        "counts": report["counts"],
                        "audit_artifact": ref,
                    }
                )
            )
            return 4 if report["failures"] else 0
        elif args.command == "chunk":
            chunk_set = build_chunks(
                args.raw_root,
                args.parsed_root,
                args.identifier,
                load_chunk_config(args.strategy),
                load_structure_rules(),
            )
            ref = ArtifactStore(args.chunk_root).put_json(chunk_set)
            print(
                json.dumps(
                    {
                        "status": "chunked",
                        "strategy": args.strategy,
                        "chunk_set_artifact": ref,
                        "searchable_chunks": sum(n["searchable"] for n in chunk_set["chunks"]),
                        "context_status": chunk_set["context_status"],
                    }
                )
            )
        elif args.command == "chunk-cite":
            if args.chunk_id is None:
                raise ValueError("chunk-cite requires --chunk-id")
            chunk_set = checked_chunks(
                args.raw_root, args.parsed_root, args.chunk_root, args.identifier
            )
            bundle = expand_chunk(chunk_set, args.chunk_id)
            print(
                json.dumps(
                    {
                        "status": "located",
                        "citations": cite_chunk(
                            args.raw_root, args.parsed_root, chunk_set, args.chunk_id
                        ),
                        "context_chunk_ids": [n["chunk_id"] for n in bundle["context"]],
                    }
                )
            )
        elif args.command == "review-chunks":
            if args.output is None or args.compare is None:
                raise ValueError("review-chunks requires --compare and a new --output path")
            sets = [
                checked_chunks(args.raw_root, args.parsed_root, args.chunk_root, d)
                for d in (args.identifier, args.compare)
            ]
            render_review(sets[0], sets[1], args.output)
            print(json.dumps({"status": "review_saved", "output": args.output.as_posix()}))
        elif args.command == "parse":
            document = parse_source(
                args.raw_root, ParseRequest(snapshot_id=args.identifier, ordinal=args.ordinal)
            )
            ref = ArtifactStore(args.parsed_root).put_json(document)
            print(
                json.dumps(
                    {
                        "status": document["status"],
                        "parse_artifact": ref,
                        "source_version_id": document["source"]["source_version_id"],
                        "total_pdf_pages": document["total_pdf_pages"],
                        "text_units": len(document["units"]),
                        "regions": sum(len(u["regions"]) for u in document["units"]),
                        "text_characters": document["text_characters"],
                        "issues": document["issues"],
                        "parser_sha256": document["parser_sha256"],
                        "ocr_performed": False,
                    }
                )
            )
            return 0 if document["status"] == "parsed" else 4
        elif args.command == "cite":
            if args.find is None or not args.find.strip():
                raise ValueError("cite requires a nonblank --find excerpt")
            saved = checked_parse(args.raw_root, args.parsed_root, args.identifier)
            if not 0 <= args.unit < len(saved["units"]):
                raise ValueError("unit position is outside the parsed document")
            text = saved["units"][args.unit]["text"]
            if text.count(args.find) != 1:
                raise ValueError("excerpt must occur exactly once in the specified unit")
            start = text.index(args.find)
            citation = make_citation(
                args.raw_root,
                args.parsed_root,
                args.identifier,
                args.unit,
                start,
                start + len(args.find),
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(citation.model_dump_json(indent=2) + "\n")
            print(citation.model_dump_json())
        else:
            citation = Citation.model_validate_json(Path(args.identifier).read_bytes())
            print(json.dumps(resolve_citation(args.raw_root, args.parsed_root, citation)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "parse_operation_failed", "error_type": type(error).__name__}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

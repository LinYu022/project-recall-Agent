"""Build a pinned local index, retrieve with traces, or replay authorized evidence."""

import argparse
import json
from pathlib import Path
from time import perf_counter

from entities.normalize import EntityRules
from entities.scopes import load_scope_rules
from retrieval.contracts import BuildRequest, RetrievalConfig, SearchRequest
from retrieval.corpus import build_corpus
from retrieval.index import build_index, checked_index
from retrieval.models import LocalModel
from retrieval.service import RetrievalService
from retrieval.vector_store import QdrantVectors
from runtime.artifacts import ArtifactStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "search", "expand"])
    parser.add_argument("request", type=Path)
    parser.add_argument("--index", help="pinned index artifact SHA256")
    parser.add_argument("--document-id")
    parser.add_argument("--include-related", action="store_true")
    parser.add_argument("--raw-root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--parsed-root", type=Path, default=Path(".local/parsed-sources"))
    parser.add_argument("--chunk-root", type=Path, default=Path(".local/chunk-sets"))
    parser.add_argument("--index-root", type=Path, default=Path(".local/retrieval/indexes"))
    parser.add_argument("--qdrant-root", type=Path, default=Path(".local/retrieval/qdrant"))
    parser.add_argument("--trace-root", type=Path, default=Path(".local/retrieval/traces"))
    parser.add_argument("--models-root", type=Path, default=Path(".local/retrieval/models"))
    args = parser.parse_args(argv)
    started = perf_counter()
    try:
        if args.command == "build":
            corpus = build_corpus(
                args.raw_root,
                args.parsed_root,
                args.chunk_root,
                BuildRequest.model_validate_json(args.request.read_bytes()),
                EntityRules.model_validate_json(
                    Path("examples/retrieval-entities.json").read_bytes()
                ),
                load_scope_rules(),
                json.loads(Path("examples/retrieval-pdf-scope.json").read_bytes()),
            )
            index = build_index(
                corpus,
                LocalModel("encoder", args.models_root),
                RetrievalConfig.model_validate_json(
                    Path("examples/retrieval-config.json").read_bytes()
                ),
            )
            ref = ArtifactStore(args.index_root).put_json(index)
            vector_store = QdrantVectors(index, ref["sha256"], args.qdrant_root, create=True)
            vector_store.close()
            server_config = {
                "schema_version": "retrieval-settings/0.1",
                "index_artifact_sha256": ref["sha256"],
                "index_root": args.index_root.as_posix(),
                "qdrant_root": args.qdrant_root.as_posix(),
                "models_root": args.models_root.as_posix(),
                "trace_root": args.trace_root.as_posix(),
                "raw_root": args.raw_root.as_posix(),
                "parsed_root": args.parsed_root.as_posix(),
                "chunk_root": args.chunk_root.as_posix(),
                "allowed_source_ids": sorted(
                    {d["metadata"]["source_id"] for d in corpus["documents"]}
                ),
            }
            config_ref = ArtifactStore(args.index_root / "server-settings").put_json(server_config)
            config_path = (
                args.index_root / "server-settings" / "artifacts" / (config_ref["sha256"] + ".json")
            )
            print(
                json.dumps(
                    {
                        "status": "indexed",
                        "dataset_build_id": corpus["dataset_build_id"],
                        "index_artifact": ref,
                        "server_config_path": config_path.as_posix(),
                        "documents": len(corpus["documents"]),
                        "embedding_usage": index["embedding_usage"],
                        "build_ms": (perf_counter() - started) * 1000,
                    }
                )
            )
            return 0
        if args.index is None:
            raise ValueError("search/expand requires a pinned --index digest")
        index = checked_index(args.index_root, args.index)
        request = SearchRequest.model_validate_json(args.request.read_bytes())
        encoder = (
            LocalModel("encoder", args.models_root)
            if args.command == "search" and request.method != "bm25"
            else None
        )
        reranker = (
            LocalModel("reranker", args.models_root)
            if args.command == "search" and request.method == "hybrid_rerank"
            else None
        )
        vector_store = QdrantVectors(index, args.index, args.qdrant_root)
        service = RetrievalService(
            index, encoder=encoder, reranker=reranker, vector_store=vector_store
        )
        if args.command == "search":
            result = service.search(request)
        else:
            if args.document_id is None:
                raise ValueError("expand requires --document-id")
            result = service.expand(
                request,
                args.document_id,
                raw_root=args.raw_root,
                parsed_root=args.parsed_root,
                chunk_root=args.chunk_root,
                include_related=args.include_related,
            )
        ref = ArtifactStore(args.trace_root).put_json(result)
        vector_store.close()
        print(
            json.dumps(
                {
                    "status": result.get("status", "expanded"),
                    "trace_artifact": ref,
                    "dataset_build_id": index["dataset_build_id"],
                    "scope_counts": result.get("scope_counts"),
                    "matched_ids": [h["document_id"] for h in result.get("matched_hits", [])],
                    "pending_ids": [h["document_id"] for h in result.get("pending_hits", [])],
                }
            )
        )
        return 0
    except (OSError, ValueError, PermissionError, RuntimeError) as error:
        print(
            json.dumps({"status": "retrieval_operation_failed", "error_type": type(error).__name__})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

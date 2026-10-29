"""Seal, verify, publish, roll back, bind and inspect version-pinned local releases."""

import argparse
import json
from pathlib import Path

import sqlalchemy as sa

from database.config import database_url
from database.schema import dataset_releases, release_defaults
from entities.scopes import load_scope_rules
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from releases.bundle import BundleStore, read_json
from releases.catalog import Catalog
from releases.contracts import BindTask, PublishRequest, ReleaseQuery, ReportReference
from releases.service import PublishedService
from retrieval.index import checked_index
from retrieval.models import LocalModel


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "build",
            "verify",
            "publish",
            "rollback",
            "bind",
            "query",
            "expand",
            "finish",
            "reference",
            "collect",
            "list",
        ],
    )
    parser.add_argument(
        "input", nargs="?", help="index/manifest digest, task/release ID, or request JSON"
    )
    parser.add_argument("--root", type=Path, default=Path(".local/releases"))
    parser.add_argument("--index-root", type=Path, default=Path(".local/retrieval/indexes"))
    parser.add_argument("--raw-root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--parsed-root", type=Path, default=Path(".local/parsed-sources"))
    parser.add_argument("--chunk-root", type=Path, default=Path(".local/chunk-sets"))
    parser.add_argument("--model-root", type=Path, default=Path(".local/retrieval/models"))
    parser.add_argument("--document-id")
    parser.add_argument(
        "--execute", action="store_true", help="actually collect an eligible independent bundle"
    )
    args = parser.parse_args(argv)
    store = BundleStore(args.root)
    engine = None
    try:
        if args.command != "list" and args.input is None:
            raise ValueError("input required")
        if args.command == "build":
            index = checked_index(args.index_root, args.input)
            result = store.create(
                args.index_root,
                args.input,
                raw_root=args.raw_root,
                parsed_root=args.parsed_root,
                chunk_root=args.chunk_root,
                model_root=args.model_root,
                encoder_identity=LocalModel("encoder", args.model_root).identity,
                reranker_identity=LocalModel("reranker", args.model_root).identity,
                rules={
                    "entity": index["corpus"]["recipe"]["entity_rules"],
                    "scope": load_scope_rules().model_dump(mode="json"),
                    "pdf": read_json(Path("examples/retrieval-pdf-scope.json")),
                    "structure": load_structure_rules().model_dump(mode="json"),
                    "chunk": load_chunk_config(
                        index["corpus"]["recipe"]["request"]["chunk_strategy"]
                    ).model_dump(mode="json"),
                },
            )
        elif args.command == "verify":
            manifest = store.verify(args.input)
            result = {
                "status": "verified",
                "release_id": manifest["release_id"],
                "files": len(manifest["inventory"]),
            }
        else:
            engine = sa.create_engine(
                database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
            )
            catalog = Catalog(engine, store)
            if args.command in {"publish", "rollback"}:
                request = PublishRequest.model_validate_json(Path(args.input).read_bytes())
                request = request.model_copy(
                    update={"action": "rollback" if args.command == "rollback" else "publish"}
                )
                result = catalog.publish(request)
            elif args.command == "bind":
                result = catalog.bind(BindTask.model_validate_json(Path(args.input).read_bytes()))
            elif args.command in {"query", "expand"}:
                if args.command == "expand" and args.document_id is None:
                    raise ValueError("expand requires --document-id")
                result = PublishedService(catalog, args.root / "traces").query(
                    ReleaseQuery.model_validate_json(Path(args.input).read_bytes()),
                    document_id=args.document_id if args.command == "expand" else None,
                )
                result = {
                    "status": result.get("status", "expanded"),
                    "release_id": result["release_id"],
                    "trace_artifact": result["trace_artifact"],
                }
            elif args.command == "finish":
                catalog.finish(args.input)
                result = {"status": "finished", "task_id": args.input}
            elif args.command == "reference":
                catalog.reference(
                    ReportReference.model_validate_json(Path(args.input).read_bytes())
                )
                result = {"status": "reference_saved"}
            elif args.command == "collect":
                result = catalog.collect(args.input, execute=args.execute)
            else:
                with engine.connect() as connection:
                    result = {
                        "default_release_id": connection.scalar(
                            sa.select(release_defaults.c.release_id)
                        ),
                        "releases": [
                            dict(row)
                            for row in connection.execute(
                                sa.select(
                                    dataset_releases.c.release_id,
                                    dataset_releases.c.dataset_build_id,
                                    dataset_releases.c.state,
                                    dataset_releases.c.retain_until,
                                )
                            ).mappings()
                        ],
                    }
        print(json.dumps(result, default=str, ensure_ascii=False))
        return 0
    except (OSError, ValueError, sa.exc.SQLAlchemyError) as error:
        print(
            json.dumps({"status": "release_operation_failed", "error_type": type(error).__name__})
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""Inspect/project entity mappings or run the predefined complaint count."""

import argparse
import json
from pathlib import Path

import sqlalchemy as sa

from database.config import database_url
from entities.counts import ComplaintCountRequest, count_complaints
from entities.links import LinkRequest, assess_links, save_assessment
from entities.normalize import load_rules, project_record
from entities.scopes import RecordLocator, build_scope, load_scope_rules
from entities.store import project_versions
from ingestion.records import prepare_import


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "project", "count", "scope", "link"])
    parser.add_argument("identifier", help="snapshot_id, or a count request JSON file")
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--ordinal", type=int, default=0, help="row position for scope inspection")
    parser.add_argument("--artifacts", type=Path, default=Path(".local/entity-links"))
    args = parser.parse_args()
    engine = None
    result: dict
    try:
        rules = load_rules()
        if args.command == "scope":
            scope = build_scope(
                args.root,
                RecordLocator(snapshot_id=args.identifier, ordinal=args.ordinal),
                load_scope_rules(),
            )
            result = {"status": "profiled", "scope": scope}
        elif args.command == "link":
            link_request = LinkRequest.model_validate_json(Path(args.identifier).read_bytes())
            assessment = assess_links(args.root, link_request, load_scope_rules())
            artifact = save_assessment(args.artifacts, assessment)
            result = {
                "status": "assessed",
                "assessment_id": assessment["assessment_id"],
                "artifact": artifact.as_posix(),
                "links": [
                    {k: link[k] for k in ("relation_type", "confirmation_status", "stage")}
                    for link in assessment["links"]
                ],
                "unknown_conditions": assessment["links"][0]["unknown_conditions"],
                "causation": "not_established",
            }
        elif args.command == "inspect":
            batch = prepare_import(args.root, args.identifier)
            projections = [project_record(row.version, rules) for row in batch.rows if row.version]
            result = {
                "status": "inspected",
                "rules_version": rules.rules_version,
                "rules_sha256": rules.sha256,
                "source_snapshot_id": args.identifier,
                "projection_count": len(projections),
                "vehicles": sum(len(p["vehicles"]) for p in projections),
                "component_positions": sum(len(p["components"]) for p in projections),
                "unknown_component_positions": sum(
                    c["canonical"] is None for p in projections for c in p["components"]
                ),
            }
        else:
            engine = sa.create_engine(
                database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
            )
            if args.command == "project":
                batch = prepare_import(args.root, args.identifier)
                result = project_versions(
                    engine, [row.version["version_id"] for row in batch.rows if row.version], rules
                )
            else:
                request = ComplaintCountRequest.model_validate_json(
                    Path(args.identifier).read_bytes()
                )
                result = count_complaints(engine, args.root, request, rules)
    except (OSError, ValueError, KeyError, sa.exc.SQLAlchemyError) as error:
        print(json.dumps({"status": "entity_operation_failed", "error_type": type(error).__name__}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result))
    return 4 if result["status"] in {"partial", "unsupported"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

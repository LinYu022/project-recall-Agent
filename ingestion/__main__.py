"""Capture or verify raw source snapshots, without running an investigation."""

import argparse
import json
from pathlib import Path

import sqlalchemy as sa

from database.config import database_url
from ingestion.importer import import_snapshot
from ingestion.records import prepare_import
from ingestion.snapshots import SnapshotStore
from ingestion.sources import load_sources
from ingestion.temporal import TemporalRequest, select_versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "verify", "inspect", "import", "select"])
    parser.add_argument("identifier", help="source_id for capture; snapshot_id for verify")
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--output", type=Path, help="Save a temporal selection to a new file")
    args = parser.parse_args()
    engine = None
    try:
        store = SnapshotStore(args.root)
        if args.command == "select":
            request = TemporalRequest.model_validate_json(Path(args.identifier).read_bytes())
            selection = select_versions(args.root, request)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(selection, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        key: value
                        for key, value in selection.items()
                        if key not in {"selected", "excluded", "inputs"}
                    }
                )
            )
            return {"selected": 0, "empty": 0, "partial": 4, "unsupported": 5}[selection["status"]]
        if args.command in {"inspect", "import"}:
            if args.command == "inspect":
                report = prepare_import(args.root, args.identifier).report
            else:
                engine = sa.create_engine(
                    database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
                )
                report = import_snapshot(engine, args.root, args.identifier)
            print(json.dumps(report))
            return 4 if report["rejected"] else 0
        if args.command == "capture":
            snapshot = store.capture(load_sources()[args.identifier])
        else:
            snapshot = store.verify(args.identifier)
    except (OSError, ValueError, KeyError, sa.exc.SQLAlchemyError) as error:
        print(
            json.dumps({"status": "snapshot_operation_failed", "error_type": type(error).__name__})
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()
    # No raw complaint text, VIN, HTTP body, or local full path in command output.
    print(snapshot.model_dump_json())
    return 0 if snapshot.status == "captured" else 3


if __name__ == "__main__":
    raise SystemExit(main())

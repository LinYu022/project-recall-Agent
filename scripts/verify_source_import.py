"""Import saved official snapshots into a disposable PostgreSQL schema twice."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from database.schema import record_versions, snapshot_imports, snapshot_rows, source_snapshots
from domain.tasks import EventInterval
from entities.counts import ComplaintCountRequest
from entities.normalize import load_rules
from ingestion.importer import first_observed_at
from ingestion.records import prepare_import
from ingestion.snapshots import SnapshotStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index", type=Path, default=Path("docs/data/raw-snapshot-observations-2026-10-26.json")
    )
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument(
        "--output", type=Path, default=Path(".local/source-import-verification.json")
    )
    parser.add_argument(
        "--entity-checks",
        action="store_true",
        help="Also project entities and verify predefined counts",
    )
    parser.add_argument("--query-checks", action="store_true", help="Also verify real query HTTP")
    args = parser.parse_args()
    if args.query_checks and not args.entity_checks:
        parser.error("--query-checks requires --entity-checks")
    index = json.loads(args.index.read_text(encoding="utf-8"))
    snapshots = [
        SnapshotStore(args.root).verify(item["snapshot_id"]) for item in index["snapshots"]
    ]
    assert [s.model_dump(mode="json") for s in snapshots] == index["snapshots"]
    url = make_url(os.environ["RECALL_TEST_DATABASE_URL"])
    if url.drivername != "postgresql+psycopg":
        raise ValueError("test database must use postgresql+psycopg")
    schema = "verify_sources_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    commands = []
    entity_results = None
    query_results = None
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        engine = sa.create_engine(scoped_url, hide_parameters=True)
        environment = os.environ | {
            "RECALL_DATABASE_URL": scoped_url.render_as_string(hide_password=False)
        }

        def run(arguments, allowed_codes=(0,)):
            result = subprocess.run(
                [sys.executable, "-m", *arguments],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.returncode not in allowed_codes:
                raise RuntimeError(
                    f"verification command failed: {arguments[0]}; exit {result.returncode}"
                )
            commands.append({"module": arguments[0], "exit_code": result.returncode})
            return result.stdout

        run(["alembic", "upgrade", "head"])
        reports = []
        for attempt in (1, 2):
            for snapshot in snapshots:
                report = json.loads(
                    run(["ingestion", "import", snapshot.snapshot_id, "--root", str(args.root)])
                )
                assert report["rejected"] == 0
                if attempt == 2:
                    assert report["status"] == "already_imported" and report["new_versions"] == 0
                reports.append({"attempt": attempt, **report})
        with engine.connect() as connection:
            counts = {
                table.name: connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (source_snapshots, record_versions, snapshot_imports, snapshot_rows)
            }
            assert counts["source_snapshots"] == counts["snapshot_imports"] == len(snapshots)
            assert counts["snapshot_rows"] == sum(
                r["input_units"] for r in reports if r["attempt"] == 1
            )
            versions = connection.execute(sa.select(record_versions)).mappings().all()
            assert counts["record_versions"] == sum(r["new_versions"] for r in reports)
            assert all(row["normalized"]["version_public_at"] is None for row in versions)
            for snapshot in snapshots:
                expected = prepare_import(args.root, snapshot.snapshot_id)
                actual = connection.scalars(
                    sa.select(snapshot_rows.c.version_id)
                    .where(snapshot_rows.c.snapshot_id == snapshot.snapshot_id)
                    .order_by(snapshot_rows.c.ordinal)
                ).all()
                assert actual == [row.version["version_id"] for row in expected.rows]
                for row in expected.rows:
                    observed = first_observed_at(connection, row.version["version_id"])
                    assert observed <= snapshot.fetched_at
                    stored = connection.scalar(
                        sa.select(record_versions.c.payload).where(
                            record_versions.c.version_id == row.version["version_id"]
                        )
                    )
                    assert stored == row.version["payload"]
        if args.entity_checks:
            rules = load_rules()
            projection_reports = []
            for attempt in (1, 2):
                for snapshot in snapshots:
                    report = json.loads(
                        run(["entities", "project", snapshot.snapshot_id, "--root", str(args.root)])
                    )
                    if attempt == 2:
                        assert report["new_projections"] == 0
                    projection_reports.append({"attempt": attempt, **report})
            count_request = ComplaintCountRequest.model_validate_json(
                Path("examples/count-complaints.json").read_bytes()
            )
            variants = {
                "all_components": count_request.model_copy(update={"components": []}),
                "power_train_or_brakes": count_request,
                "service_brakes": count_request.model_copy(
                    update={"components": ["SERVICE BRAKES"]}
                ),
            }
            variants["production_date_unknown"] = count_request.model_copy(
                update={
                    "production_interval": EventInterval(
                        start=date(2015, 1, 1), end=date(2017, 12, 31)
                    )
                }
            )
            count_reports = {}
            request_dir = args.output.parent / ("entity-requests-" + uuid4().hex)
            request_dir.mkdir(parents=True)
            for name, variant in variants.items():
                path = request_dir / f"{name}.json"
                path.write_text(variant.model_dump_json(), encoding="utf-8")
                count_reports[name] = json.loads(
                    run(
                        ["entities", "count", str(path), "--root", str(args.root)],
                        allowed_codes=(0, 4),
                    )
                )
            # Independent raw-row reference uses original API labels and ODI keys, no projection tables.
            raw_rows = []
            for snapshot in snapshots:
                if snapshot.source.kind == "complaint_api":
                    raw_rows.extend(
                        json.loads(SnapshotStore(args.root).raw_bytes(snapshot.raw))["results"]
                    )
            checks = {
                "all_components": len({row["odiNumber"] for row in raw_rows}),
                "power_train_or_brakes": len(
                    {
                        row["odiNumber"]
                        for row in raw_rows
                        if {"POWER TRAIN", "SERVICE BRAKES"}
                        & {label.strip() for label in row["components"].split(",")}
                    }
                ),
                "service_brakes": len(
                    {
                        row["odiNumber"]
                        for row in raw_rows
                        if "SERVICE BRAKES"
                        in {label.strip() for label in row["components"].split(",")}
                    }
                ),
            }
            for name, expected in checks.items():
                assert count_reports[name]["status"] == "counted"
                assert count_reports[name]["distinct_complaints"] == expected
            assert count_reports["production_date_unknown"]["status"] == "partial"
            assert count_reports["production_date_unknown"]["distinct_complaints"] == 0
            assert (
                count_reports["production_date_unknown"]["unknown_production_date_versions"]
                == checks["power_train_or_brakes"]
            )
            entity_results = {
                "rules_version": rules.rules_version,
                "rules_sha256": rules.sha256,
                "projection_reports": projection_reports,
                "count_reports": count_reports,
                "independent_raw_reference_counts": checks,
            }
            if args.query_checks:
                query_output = args.output.parent / "query-http-official.json"
                run(
                    [
                        "scripts.verify_query_http",
                        "--saved-official",
                        "--root",
                        str(args.root),
                        "--output",
                        str(query_output),
                    ]
                )
                query_results = json.loads(query_output.read_bytes())
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_postgresql_import_of_previously_downloaded_official_snapshots",
        "mapping_version": reports[0]["mapping_version"],
        "schema_cleaned_up": True,
        "new_https_requests": 0,
        "external_model_calls": 0,
        "reports": reports,
        "counts_after_two_passes": counts,
        "payload_readback_equal": True,
        "raw_pointer_links_equal": True,
        "publication_times_known": False,
        "commands": commands,
        "entity_checks": entity_results,
        "query_http_checks": query_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "passed", "counts": counts, "output": args.output.as_posix()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

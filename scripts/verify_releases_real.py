"""Real PostgreSQL + saved official sources + real local models, in an isolated schema."""

import argparse
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import sqlalchemy as sa
import uvicorn
from alembic import command
from alembic.config import Config

from app.release_api import create_release_app
from database.schema import release_defaults
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import BindTask, PublishRequest, ReleaseQuery, ReportReference
from releases.service import PublishedService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=Path(".local/release-candidates.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/release-real.json"))
    args = parser.parse_args()
    data = json.loads(args.candidates.read_bytes())
    first, second = data["candidates"]["baseline"], data["candidates"]["expanded"]
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    schema = "release_check_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    server = None
    listener = None
    thread = None
    checks = []
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": f"-csearch_path={schema}"},
        )
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        store = BundleStore(Path(data["root"]))
        catalog = Catalog(engine, store)

        def publish(candidate, expected, action="publish"):
            result = catalog.publish(
                PublishRequest(
                    release_id=candidate["release_id"],
                    manifest_sha256=candidate["manifest_sha256"],
                    expected_default=expected,
                    retain_until=datetime.now(UTC) + timedelta(days=30),
                    action=action,
                )
            )
            checks.append(
                {
                    "case": action,
                    "release_id": result["release_id"],
                    "previous_default": result["previous_default"],
                }
            )

        publish(first, None)
        sources = ["NHTSA-API-CAMPAIGN-10V039000", "NHTSA-INV-PE10006-CLOSE"]
        task = BindTask(
            task_id="REAL-LONG-TASK",
            allowed_source_ids=sources,
            scope={"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
        )
        catalog.bind(task)
        runner = PublishedService(catalog, Path(data["root"]) / "traces")
        query = ReleaseQuery(task_id=task.task_id, query="ABS rough road braking", method="hybrid")
        before = runner.query(query)
        publish(second, first["release_id"])
        after = runner.query(query)
        assert before["release_id"] == after["release_id"] == first["release_id"]
        assert [h["document_id"] for h in before["matched_hits"]] == [
            h["document_id"] for h in after["matched_hits"]
        ]
        checks.append(
            {
                "case": "old_task_survives_new_default",
                "release_id": after["release_id"],
                "before_trace": before["trace_artifact"],
                "after_trace": after["trace_artifact"],
            }
        )
        catalog.bind(
            BindTask(
                task_id="REAL-NEW-TASK",
                allowed_source_ids=["NHTSA-API-CAMPAIGN-23V838000"],
                scope={"make": "TESLA", "model": "MODEL Y", "model_year": 2022},
            )
        )
        new = runner.query(
            ReleaseQuery(
                task_id="REAL-NEW-TASK", query="Autosteer misuse software update", method="hybrid"
            )
        )
        assert new["release_id"] == second["release_id"] and new["matched_hits"]
        checks.append(
            {
                "case": "new_task_uses_new_release",
                "release_id": new["release_id"],
                "trace": new["trace_artifact"],
            }
        )
        expanded = runner.query(query, document_id=after["matched_hits"][0]["document_id"])
        assert expanded["focus"]["bundle"]["focus"]["citations"]
        checks.append(
            {"case": "old_release_original_byte_citations", "trace": expanded["trace_artifact"]}
        )
        catalog.bind(
            BindTask(
                task_id="REAL-COUNT",
                allowed_source_ids=["NHTSA-COMPLAINTS-F150-2016"],
                scope={"make": "FORD", "model": "F-150", "model_year": 2016},
            )
        )
        for components, expected in [
            ([], 103),
            (["POWER TRAIN", "SERVICE BRAKES"], 86),
            (["SERVICE BRAKES"], 5),
        ]:
            counted = runner.count(ReleaseQuery(task_id="REAL-COUNT", query="count"), components)
            assert (
                counted["distinct_complaints"] == expected
                and counted["release_id"] == second["release_id"]
            )
            checks.append(
                {
                    "case": "published_distinct_count",
                    "components": components,
                    "distinct_complaints": expected,
                    "release_id": counted["release_id"],
                }
            )
        catalog.bind(
            BindTask(
                task_id="REAL-COUNT-UNKNOWN",
                allowed_source_ids=["NHTSA-COMPLAINTS-F150-2016"],
                scope={
                    "make": "FORD",
                    "model": "F-150",
                    "model_year": 2016,
                    "production_date": "2015-01-01",
                },
            )
        )
        unknown = runner.count(
            ReleaseQuery(task_id="REAL-COUNT-UNKNOWN", query="count"),
            ["POWER TRAIN", "SERVICE BRAKES"],
        )
        assert (
            unknown["status"] == "partial"
            and unknown["distinct_complaints"] == 0
            and unknown["unknown_production_date_versions"] == 86
        )
        checks.append(
            {
                "case": "unknown_production_date_retained",
                "status": unknown["status"],
                "unknown_versions": 86,
            }
        )
        publish(first, second["release_id"], action="rollback")
        assert (
            catalog.bind(BindTask(task_id="REAL-AFTER-ROLLBACK", allowed_source_ids=sources))[
                "release_id"
            ]
            == first["release_id"]
        )
        catalog.reference(
            ReportReference(
                reference_id="REAL-TRACE-REF",
                task_id="REAL-LONG-TASK",
                artifact_sha256=after["trace_artifact"]["sha256"],
            )
        )
        retention = catalog.collect(second["release_id"])
        assert retention["status"] == "retained" and "active_tasks" in retention["reasons"]
        checks.append({"case": "retention_protection", **retention})
        app = create_release_app(development_mode=True, service=runner)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=120, trust_env=False
        ) as client:

            def post(case, path, payload, code=200):
                response = client.post(path, json=payload)
                assert response.status_code == code, (
                    case,
                    response.status_code,
                    response.text[:200],
                )
                value = response.json()
                checks.append(
                    {
                        "case": case,
                        "http_status": code,
                        "status": value.get("status"),
                        "release_id": value.get("release_id"),
                    }
                )
                return value

            post("http_old_task_search", "/published/search", query.model_dump(mode="json"))
            post(
                "http_unbound_rejected",
                "/published/search",
                {"task_id": "NOT-BOUND", "query": "brake"},
                409,
            )
            post(
                "http_mixed_release_rejected",
                "/published/search",
                query.model_dump(mode="json") | {"expected_release_id": second["release_id"]},
                409,
            )
            post(
                "http_input_scope_override_rejected",
                "/published/search",
                query.model_dump(mode="json") | {"scope": {"model_year": 2011}},
                422,
            )
            post(
                "http_task_source_escalation_rejected",
                "/tasks/bind",
                {"task_id": "OUTSIDE-SOURCE", "allowed_source_ids": ["NOT-IN-RELEASE"]},
                403,
            )
            value = post(
                "http_published_count",
                "/published/complaints/count",
                {
                    "request": {"task_id": "REAL-COUNT", "query": "count"},
                    "components": ["SERVICE BRAKES"],
                },
            )
            assert value["distinct_complaints"] == 5
            link_sources = ["NHTSA-API-CAMPAIGN-22V150000", "NHTSA-API-CAMPAIGN-20V332000"]
            catalog.bind(
                BindTask(
                    task_id="REAL-LINKS",
                    release_id=second["release_id"],
                    allowed_source_ids=link_sources,
                    scope={"make": "FORD", "model": "F-150", "model_year": 2016},
                )
            )
            link_body = {
                "request": {"task_id": "REAL-LINKS", "query": "official reference"},
                "left": {"snapshot_id": "e38fe78667a34a27befd8af43c85941f", "ordinal": 0},
                "right": {"snapshot_id": "a383e549c8274613a765266427173a1d", "ordinal": 0},
            }
            linked = post("http_published_official_reference", "/published/entity-links", link_body)
            assert linked["status"] == "assessed" and linked["release_id"] == second["release_id"]
            relation = next(
                r
                for r in linked["assessment"]["links"]
                if r["relation_type"] == "officially_referenced"
            )
            assert relation["confirmation_status"] == "confirmed"
            post(
                "http_link_endpoint_outside_task",
                "/published/entity-links",
                link_body
                | {"left": {"snapshot_id": "61cd818ad3ae45c7bbdc94ecf0b479bc", "ordinal": 0}},
                403,
            )
            post(
                "http_original_expansion",
                "/published/expand",
                {
                    "request": query.model_dump(mode="json"),
                    "document_id": after["matched_hits"][0]["document_id"],
                },
            )
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.select(release_defaults.c.release_id)) == first["release_id"]
            )
        result = {
            "timestamp": datetime.now(UTC).isoformat(),
            "classification": "real_postgresql_saved_official_sources_real_onnx_and_qdrant_task_release_replay",
            "candidates": data["candidates"],
            "checks": checks,
            "new_source_requests": 0,
            "external_generative_model_calls": 0,
            "default_at_end": first["release_id"],
            "local_inference": "real MiniLM embedding queries; no model quality experiment in this module",
        }
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=15)
            assert not thread.is_alive()
        if listener is not None:
            listener.close()
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()
    result.update(http_server_stopped=True, disposable_schema_removed=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "release_real_checks": len(checks),
                "http_server_stopped": True,
                "schema_removed": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

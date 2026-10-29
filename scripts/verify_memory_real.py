"""Retained official sources + real extraction process; confirmations/generation are synthetic."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from control.contracts import Plan, Submission
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.service import TaskControl
from control.worker import Worker
from ingestion.records import content_hash
from investigation.contracts import FixedConfig
from memory import schema as db
from memory.contracts import Change, ProjectInput, Review, Search, SourceInput
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService


class DemonstrationModel:
    identity = {"model_id": "scripted-memory-abstention", "is_simulated": True}

    def __init__(self):
        self.inputs = []

    def generate(self, system, payload, *, max_tokens):
        self.inputs.append(payload)
        return {
            "identity": self.identity,
            "accepted_complete": True,
            "proposal": {
                "disposition": "insufficient_evidence",
                "claims": [],
                "unresolved_fields": ["individual_vehicle", "causality"],
            },
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "estimated_cost": None,
            "elapsed_ms": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/P1-13/official-memory.json"))
    args = parser.parse_args()
    root = Path(".local/P1-13") / ("demo-" + uuid4().hex[:8])
    url = make_url(os.environ["RECALL_TEST_DATABASE_URL"])
    schema = "memory_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    result = {
        "classification": "retained_official_NHTSA_release_real_postgresql_and_extractor_process_synthetic_confirmations_and_generation",
        "timestamp": datetime.now(UTC).isoformat(),
        "new_api_requests": 0,
        "new_source_requests": 0,
        "actual_provider_usage": None,
        "estimated_cost": None,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "schema_removed": False,
    }
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        scoped_url = url.update_query_dict({"options": "-csearch_path=" + schema})
        engine = sa.create_engine(
            scoped_url, hide_parameters=True, connect_args={"connect_timeout": 5}
        )
        with engine.begin() as connection:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        c = json.loads(Path("examples/release-candidates.json").read_bytes())["candidates"][
            "expanded"
        ]
        catalog = Catalog(engine, BundleStore(Path(".local/releases")))
        catalog.publish(
            PublishRequest(
                release_id=c["release_id"],
                manifest_sha256=c["manifest_sha256"],
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        model = DemonstrationModel()
        control = TaskControl(
            PublishedService(catalog, root / "traces"),
            model,
            root / "runs",
            config=FixedConfig(retrieval_method="hybrid", prompt_characters=32000),
        )
        m = control.memory
        m.project(
            "synthetic-user",
            ProjectInput(
                project_id="recall-demo", name="SYNTHETIC controlled memory demonstration"
            ),
        )
        case = next(
            c
            for c in json.loads(Path("evals/investigation-v0.1/cases.json").read_bytes())["cases"]
            if c["case_id"] == "DEV-PRIUS-JOINT"
        )
        declarations = {
            "memories": [
                {
                    "kind": "preference",
                    "scope": "project",
                    "project_id": "recall-demo",
                    "semantic_key": "response.language",
                    "value": "SYNTHETIC 请使用中文并列出未知项",
                },
                {
                    "kind": "rule",
                    "scope": "project",
                    "project_id": "recall-demo",
                    "semantic_key": "investigation.applicability",
                    "release_id": c["release_id"],
                    "sources": [{"source_id": case["sources"][0]}],
                    "policy_id": "keep_applicability_unknown",
                },
                {
                    "kind": "source_pointer",
                    "scope": "project",
                    "project_id": "recall-demo",
                    "semantic_key": "source.entry",
                    "release_id": c["release_id"],
                    "sources": [{"source_id": case["sources"][0]}],
                },
            ]
        }
        source = m.source(
            "synthetic-user",
            SourceInput(
                project_id="recall-demo",
                origin="user_statement",
                text=json.dumps(declarations, ensure_ascii=False),
            ),
        )
        job = m.enqueue("synthetic-user", source["source_id"])
        q = Search(
            project_id="recall-demo",
            release_id=c["release_id"],
            allowed_source_ids=case["sources"],
            scope=case["scope"],
            query="中文 applicability",
        )
        assert not m.search("synthetic-user", q)["entries"]
        environment = dict(os.environ)
        environment["RECALL_DATABASE_URL"] = scoped_url.render_as_string(hide_password=False)
        process = subprocess.run(
            [sys.executable, "-m", "memory", "--once"],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        result["extractor_process_exit_code"] = process.returncode
        assert process.returncode == 0, "extractor_process_failed"
        extracted = m.extraction("synthetic-user", job["job_id"])
        assert extracted["state"] == "completed" and len(extracted["candidate_ids"]) == 3
        assert not m.search("synthetic-user", q)["entries"]
        result["queued_candidates_only_until_review"] = True
        preference_id = None
        for candidate in m.candidates("synthetic-user"):
            promoted = m.review(
                "synthetic-user",
                candidate["candidate_id"],
                Review(
                    expected_revision=1,
                    proposal_sha256=candidate["proposal_sha256"],
                    decision="approve",
                    note="SYNTHETIC verification approval; not independent human review",
                ),
            )
            if candidate["proposal"]["kind"] == "preference":
                preference_id = promoted["memory_id"]
        assert len(m.search("synthetic-user", q)["entries"]) == 3
        assert m.search("synthetic-user", q)["cache_hit"]
        d = Dispatcher(control)
        runs = []
        for name in ["BEFORE", "AFTER"]:
            ready = control.submit(
                "synthetic-user",
                Submission(
                    task_id=name,
                    plan=Plan.model_validate(
                        {
                            "release_id": c["release_id"],
                            "allowed_source_ids": case["sources"],
                            "scope": case["scope"],
                            "request": case["request"] | {"task_id": name},
                            "mode": "EXECUTE",
                            "memory": {"project_id": "recall-demo"},
                        }
                    ),
                ),
            )
            queued = d.enqueue(
                "synthetic-user",
                name,
                Enqueue(expected_version=ready["version"], idempotency_key="memory-demo"),
            )
            done = Worker(d, "memory-demo").run_once(job_id=queued["job_id"])
            assert done["task"]["state"] == "COMPLETED", done["task"]["state"]
            report = control.report("synthetic-user", name)
            with catalog.transaction() as connection:
                published_ref, original_report = control._published_report(
                    connection, "synthetic-user", name
                )
            runs.append(
                {
                    "task_id": name,
                    "disposition": report["proposal"]["disposition"],
                    "model_calls": report["model_calls"],
                    "memory_references": len(report["memory_usage"]["references"]),
                    "origin_replays": report["evidence_verification"]["origin_replays"],
                    "report_content_sha256": content_hash(original_report),
                    "published_report": published_ref,
                    "resources": report["resources"],
                    "elapsed_ms": report["total_elapsed_ms"],
                }
            )
            if name == "BEFORE":
                assert preference_id
                m.change(
                    "synthetic-user",
                    preference_id,
                    Change(expected_revision=1, action="delete", note="SYNTHETIC correction"),
                )
                assert not any(
                    e["memory_id"] == preference_id
                    for e in m.search("synthetic-user", q)["entries"]
                )
        assert runs[0]["memory_references"] == 3 and runs[1]["memory_references"] == 2
        old = control.report("synthetic-user", "BEFORE")
        assert (
            next(a for a in old["memory_audit"] if a["memory_id"] == preference_id)["current_state"]
            == "deleted"
        )
        with engine.connect() as connection:
            result["active_index_after_deletion"] = connection.scalar(
                sa.select(sa.func.count()).select_from(db.index)
            )
            result["adoption_records"] = connection.scalar(
                sa.select(sa.func.count()).select_from(db.adoptions)
            )
        result |= {
            "release_id": c["release_id"],
            "case_id": case["case_id"],
            "runs": runs,
            "historical_report_marks_deletion": True,
            "new_task_omits_deleted_preference": True,
            "source_pointers_not_facts": True,
            "root": root.as_posix(),
            "generation_mode": "scripted_simulation",
        }
    finally:
        if engine:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()
        result["schema_removed"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"output": args.output.as_posix(), "schema_removed": True, "new_api_requests": 0}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

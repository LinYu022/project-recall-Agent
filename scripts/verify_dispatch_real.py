"""Actual worker-process exit/restart on retained NHTSA sources; generation is simulated."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from control.contracts import Plan, Submission
from control.dispatch import Dispatcher, db_now
from control.queue_contracts import Enqueue
from control.queue_schema import calls, jobs
from control.schema import events, executions
from control.service import TaskControl
from control.worker import Worker
from investigation.contracts import FixedConfig
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService
from scripts.verify_investigation_real import SyntheticModel

ROOT = Path(".local/P1-11/process-runs")
CONFIG = FixedConfig(max_model_calls=6, context_mode="structured")


def make_control(engine):
    return TaskControl(
        PublishedService(Catalog(engine, BundleStore(Path(".local/releases"))), ROOT / "traces"),
        SyntheticModel(),
        ROOT,
        config=CONFIG,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-job")
    parser.add_argument("--schema")
    parser.add_argument("--crash", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".local/P1-11/process-experiment.json"))
    args = parser.parse_args()
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    if args.child_job:
        if not re.fullmatch(r"dispatch_[0-9a-f]{32}", args.schema or ""):
            parser.error("generated test schema required")
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-csearch_path=" + args.schema},
        )
        try:
            dispatcher = Dispatcher(make_control(engine))

            def fault(stage, kind, ordinal):
                if args.crash and stage == "after_settle" and kind == "model":
                    os._exit(23)

            result = Worker(dispatcher, "process-" + str(os.getpid()), fault=fault).run_once(
                job_id=args.child_job
            )
            assert result is not None and result["task"]["state"] == "COMPLETED"
            print(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "state": result["task"]["state"],
                        "calls": result["calls"],
                        "external_model_requests": 0,
                    }
                )
            )
        finally:
            engine.dispose()
        return 0
    schema = "dispatch_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    created = False
    result = {
        "schema_version": "dispatch-process-experiment/0.1",
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_postgresql_real_worker_process_exit_retained_official_sources_simulated_generation",
        "new_source_requests": 0,
        "new_api_requests": 0,
        "formal_semantic_metrics": None,
        "schema_removed": False,
    }
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        created = True
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-csearch_path=" + schema},
        )
        with engine.begin() as connection:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        control = make_control(engine)
        candidate = json.loads(Path("examples/release-candidates.json").read_bytes())["candidates"][
            "expanded"
        ]
        control.catalog.publish(
            PublishRequest(
                **{k: candidate[k] for k in ("release_id", "manifest_sha256")},
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        case = json.loads(Path("evals/investigation-v0.1/cases.json").read_bytes())["cases"][0]
        ready = control.submit(
            "local-process-operator",
            Submission(
                task_id="PROCESS-PRIUS",
                plan=Plan(
                    release_id=candidate["release_id"],
                    allowed_source_ids=case["sources"],
                    scope=case["scope"],
                    request=case["request"] | {"task_id": "PROCESS-PRIUS"},
                    mode="EXECUTE",
                ),
            ),
        )
        dispatch = Dispatcher(control)
        job = dispatch.enqueue(
            "local-process-operator",
            "PROCESS-PRIUS",
            Enqueue(expected_version=ready["version"], idempotency_key="process-crash"),
        )
        invocation = [
            sys.executable,
            "-m",
            "scripts.verify_dispatch_real",
            "--child-job",
            job["job_id"],
            "--schema",
            schema,
        ]
        first = subprocess.run(
            [*invocation, "--crash"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert first.returncode == 23, first.stderr[-1000:]
        with engine.begin() as connection:
            prior = (
                connection.execute(sa.select(jobs).where(jobs.c.job_id == job["job_id"]))
                .mappings()
                .one()
            )
            assert prior["state"] == "running"
            connection.execute(
                executions.update()
                .where(executions.c.execution_id == prior["execution_id"])
                .values(lease_until=db_now(connection) - timedelta(seconds=1))
            )
        # Reconstruct the main service objects, then use a second independent worker process.
        control = make_control(engine)
        dispatch = Dispatcher(control)
        assert dispatch.recover_expired() == [job["job_id"]]
        with engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.job_id == job["job_id"])
                .values(available_at=db_now(connection) - timedelta(seconds=1))
            )
        second = subprocess.run(
            invocation,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert second.returncode == 0, second.stderr[-1000:]
        recovered = json.loads(second.stdout)
        assert (
            recovered["calls"]["new_model_calls"] == 0
            and recovered["calls"]["replayed_model_calls"] == 1
        )
        with engine.connect() as connection:
            publications = connection.scalar(
                sa.select(sa.func.count())
                .select_from(events)
                .where(events.c.task_id == "PROCESS-PRIUS", events.c.action == "report_published")
            )
            model_calls = connection.scalar(
                sa.select(sa.func.count())
                .select_from(calls)
                .where(calls.c.job_id == job["job_id"], calls.c.kind == "model")
            )
        assert publications == model_calls == 1
        view = dispatch.view("local-process-operator", "PROCESS-PRIUS")
        assert view["jobs"][0]["state"] == "completed"
        report = control.report("local-process-operator", "PROCESS-PRIUS")
        assert report["proposal"]["disposition"] == "insufficient_evidence"
        result |= {
            "first_process_exit_code": 23,
            "second_process_exit_code": 0,
            "recovered_process": recovered,
            "successful_publications": publications,
            "durable_simulated_model_calls": model_calls,
            "resource_pool": view["resources"],
            "source_release": candidate["release_id"],
            "frozen_source_ids": case["sources"],
            "disposition": "insufficient_evidence",
            "fault_injection": [
                "worker os._exit(23) after saved simulated model response",
                "lease timestamp forced past",
                "retry availability timestamp advanced",
            ],
            "limits": [
                "Two actual worker processes; main service objects reconstructed in the parent process.",
                "Generation is explicitly simulated abstention, not real model reasoning.",
                "Unknown synthetic usage is conservatively charged; no actual provider usage or price is inferred.",
            ],
        }
    except Exception as error:
        result["failure_type"] = type(error).__name__
        result["failure_message"] = str(error)[-2000:]
        raise
    finally:
        if engine:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
            result["schema_removed"] = True
        admin.dispose()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"output": args.output.as_posix(), "schema_removed": True, "external_model_requests": 0}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

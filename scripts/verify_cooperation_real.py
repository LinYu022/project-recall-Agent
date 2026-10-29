"""Four methods on frozen official cases; scripted generation and no provider network path."""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from control.contracts import Plan, Submission
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.queue_schema import calls
from control.service import TaskControl
from control.worker import Worker
from ingestion.records import content_hash
from investigation.contracts import FixedConfig, ReferenceCase
from investigation.review import export_blind
from investigation.scoring import score
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService


class ComparisonModel:
    identity = {
        "model_id": "scripted-common-cooperation-abstention",
        "is_simulated": True,
        "version": "0.1",
    }

    def generate(self, system, payload, *, max_tokens):
        from tests.test_single_agent import decision

        proposal = {
            "disposition": "insufficient_evidence",
            "claims": [],
            "unresolved_fields": ["individual_vehicle", "causality", "source_coverage"],
        }
        if "available_tools" in payload:
            tools = payload["available_tools"]
            role = payload.get("delegation", {}).get("role")
            evidence = payload["evidence"]
            candidates = payload["candidates"]
            has_relationship = any(e["kind"] == "relationship" for e in evidence)
            text_sources = {
                s
                for e in evidence
                if e["kind"] == "text"
                for s in e["payload"].get("source_ids", [])
            }
            value = decision("finish", proposal=proposal)
            if payload["remaining_model_calls"] > 1:
                if "batch" in tools and not has_relationship and not candidates:
                    value = decision(
                        "batch",
                        batch=[
                            {"action": "search", "query": payload["initial_query"]},
                            {"action": "links"},
                        ],
                    )
                elif "links" in tools and not has_relationship:
                    value = decision("links")
                elif "count" in tools and not any(e["kind"] == "count" for e in evidence):
                    value = decision("count")
                elif "search" in tools and not candidates:
                    value = decision("search", query=payload["initial_query"])
                elif "expand" in tools:
                    target = next(
                        (c for c in candidates if c["source_id"] not in text_sources), None
                    )
                    if target:
                        value = decision("expand", document_id=target["id"], include_related=False)
            if role == "data" and value["action"] == "finish":
                value["assessments"] = [
                    {
                        "issue": "applicability",
                        "subject": "TASK",
                        "value": "unknown",
                        "support_ids": ["E001"],
                        "counter_ids": [],
                        "reason": "SIMULATED: structured records do not establish an individual vehicle's configuration.",
                    }
                ]
            proposal = value
        return {
            "identity": self.identity,
            "proposal": proposal,
            "accepted_complete": True,
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "estimated_cost": None,
            "elapsed_ms": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".local/P1-12/official-comparison.json")
    )
    parser.add_argument("--case-id", choices=["DEV-PRIUS-JOINT", "DEV-DOOR-PAIR"])
    parser.add_argument(
        "--method", choices=["fixed", "single_agent", "parallel_tools", "multi_agent"]
    )
    args = parser.parse_args()
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    schema = "cooperation_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    root = Path(".local/P1-12")
    plan = json.loads(Path("examples/cooperation-comparison.json").read_bytes())
    if args.case_id:
        plan["case_ids"] = [args.case_id]
    if args.method:
        plan["methods"] = [args.method]
    config = FixedConfig.model_validate(plan["profile"])
    run_group_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    result = {
        "schema_version": "official-cooperation-comparison/0.1",
        "timestamp": datetime.now(UTC).isoformat(),
        "run_group_id": run_group_id,
        "classification": "real_postgresql_retained_NHTSA_sources_local_models_and_scripted_generation",
        "profile": plan,
        "runs": [],
        "new_api_requests": 0,
        "new_source_requests": 0,
        "actual_provider_usage": None,
        "formal_semantic_metrics": None,
        "independent_human_reviews": 0,
        "schema_removed": False,
    }
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-csearch_path=" + schema},
        )
        with engine.begin() as connection:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        candidate = json.loads(Path("examples/release-candidates.json").read_bytes())["candidates"][
            "expanded"
        ]
        catalog = Catalog(engine, BundleStore(Path(".local/releases")))
        catalog.publish(
            PublishRequest(
                **{k: candidate[k] for k in ("release_id", "manifest_sha256")},
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        control = TaskControl(
            PublishedService(catalog, root / "traces"),
            ComparisonModel(),
            root / "runs",
            config=config,
        )
        dispatcher = Dispatcher(control)
        all_cases = json.loads(Path("evals/investigation-v0.1/cases.json").read_bytes())["cases"]
        for case in [c for c in all_cases if c["case_id"] in plan["case_ids"]]:
            for method in plan["methods"]:
                task_id = case["case_id"] + "-" + method.upper().replace("_", "-")
                ready = control.submit(
                    "local-comparison-user",
                    Submission(
                        task_id=task_id,
                        plan=Plan(
                            release_id=candidate["release_id"],
                            allowed_source_ids=case["sources"],
                            scope=case["scope"],
                            request=case["request"] | {"task_id": task_id},
                            mode="EXECUTE",
                            investigator=method,
                        ),
                    ),
                )
                job = dispatcher.enqueue(
                    "local-comparison-user",
                    task_id,
                    Enqueue(expected_version=ready["version"], idempotency_key="comparison"),
                )
                done = Worker(dispatcher, "comparison-worker").run_once(job_id=job["job_id"])
                assert done is not None
                record = {
                    "case_id": case["case_id"],
                    "method": method,
                    "task_state": done["task"]["state"],
                    "calls": done["calls"],
                }
                if done["task"]["state"] == "COMPLETED":
                    report = control.report("local-comparison-user", task_id)
                    target = root / "rounds" / run_group_id / "reports" / (task_id + ".json")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    reference = ReferenceCase.model_validate_json(
                        (
                            Path("evals/investigation-v0.2") / (case["case_id"] + ".json")
                        ).read_bytes()
                    )
                    with catalog.transaction() as connection:
                        published_ref, _ = control._published_report(
                            connection, "local-comparison-user", task_id
                        )
                    record |= {
                        "execution_status": report["execution_status"],
                        "disposition": report["proposal"]["disposition"],
                        "model_calls": report["model_calls"],
                        "tool_calls": len(report["tool_calls"]),
                        "elapsed_ms": report["total_elapsed_ms"],
                        "resources": report.get("resources"),
                        "delegation_policy": report.get("delegation_policy"),
                        "branch_results": report.get("branch_results"),
                        "branch_timing": report.get("branch_timing"),
                        "conflicts": report.get("conflicts"),
                        "tool_batches": report.get("tool_batches"),
                        "report_content_sha256": content_hash(report),
                        "report_path": target.as_posix(),
                        "published_report": published_ref,
                        "branch_effective_configs": {
                            r["delegation_id"]: json.loads(
                                (
                                    Path(report["branch_artifact_roots"][r["delegation_id"]])
                                    / "artifacts"
                                    / (r["report_artifact"]["sha256"] + ".json")
                                ).read_bytes()
                            )["config"]
                            for r in report.get("branch_results", [])
                            if r.get("report_artifact")
                        },
                        "score": score(report, reference),
                        "blind_id": export_blind(report, root / "blind"),
                    }
                else:
                    status = dispatcher.view("local-comparison-user", task_id)
                    record |= {
                        "execution_status": "failed",
                        "dispatch": status["jobs"],
                        "ledger_calls": status["calls"],
                    }
                result["runs"].append(record)
                print(
                    json.dumps(
                        {
                            k: record.get(k)
                            for k in (
                                "case_id",
                                "method",
                                "task_state",
                                "execution_status",
                                "model_calls",
                                "tool_calls",
                                "disposition",
                            )
                        }
                    ),
                    flush=True,
                )
        assert len(result["runs"]) == len(plan["case_ids"]) * len(plan["methods"])
        with engine.connect() as connection:
            result["durable_calls"] = connection.scalar(
                sa.select(sa.func.count()).select_from(calls)
            )
    except Exception as error:
        result["failure_type"] = type(error).__name__
        result["failure_message"] = str(error)[-1000:]
        raise
    finally:
        if engine:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        result["schema_removed"] = True
        admin.dispose()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return int(any(r["task_state"] != "COMPLETED" for r in result["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())

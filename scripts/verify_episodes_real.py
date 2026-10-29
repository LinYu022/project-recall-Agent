"""Official frozen sources and real history navigation; generation/status labels are scripted."""

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
from control.schema import executions
from control.service import TaskControl
from control.worker import Worker
from ingestion.records import content_hash
from investigation.contracts import FixedConfig
from memory import episode_schema as db
from memory.contracts import ProjectInput
from memory.episode_contracts import HistoryQuery, SourcePage, Window
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService


def decision(action, **changes):
    return {
        "action": action,
        "reason": "SYNTHETIC bounded history demonstration",
        "hypotheses": [],
        "evidence_gaps": ["individual conditions remain unknown"],
        "next_steps": ["check current allowed sources"],
        "query": None,
        "document_id": None,
        "proposal": None,
    } | changes


class HistoryModel:
    identity = {"model_id": "scripted-episodic-navigation", "is_simulated": True}

    def __init__(self, mode, focus_source):
        self.mode, self.focus_source, self.inputs = mode, focus_source, []

    def generate(self, system, payload, *, max_tokens):
        self.inputs.append(payload)
        texts = [e for e in payload["evidence"] if e["kind"] == "text"]
        if self.mode == "failed":
            value = decision("expand", document_id="D999")
        elif self.mode == "new" and len(self.inputs) == 1:
            value = decision("history_search", history_query="brake")
        elif self.mode == "new" and len(self.inputs) == 2:
            entry = next(
                e for e in payload["history_context"]["summaries"] if e["negative_hypotheses"]
            )
            anchor = next(a for a in entry["anchors"] if a["kind"] == "hypothesis")
            value = decision(
                "history_window",
                history_episode_id=entry["episode_id"],
                history_event_id=anchor["event_id"],
            )
        elif not payload["candidates"]:
            value = decision("search", query="brake")
        elif not texts:
            candidate = next(
                (c for c in payload["candidates"] if c["source_id"] == self.focus_source),
                payload["candidates"][0],
            )
            value = decision("expand", document_id=candidate["id"], include_related=False)
        else:
            value = decision(
                "finish",
                proposal={
                    "disposition": "insufficient_evidence",
                    "claims": [],
                    "unresolved_fields": ["individual_vehicle", "causality"],
                },
            )
            if self.mode == "old":
                value["hypotheses"] = [
                    {
                        "hypothesis_id": "H001",
                        "statement": "SIMULATED hypothesis label: a similar brake symptom proves the same defect",
                        "status": "contradicted",
                        "support_ids": [],
                        "counter_ids": [texts[0]["alias"]],
                    }
                ]
        return {
            "identity": self.identity,
            "accepted_complete": True,
            "proposal": value,
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "elapsed_ms": 0,
            "estimated_cost": None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/P1-14/official-history.json"))
    args = parser.parse_args()
    schema, root = "episodes_" + uuid4().hex, Path(".local/P1-14") / ("demo-" + uuid4().hex[:8])
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    result = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_postgresql_official_NHTSA_sources_local_models_and_history_tools_scripted_generation",
        "new_api_requests": 0,
        "new_source_requests": 0,
        "actual_provider_usage": None,
        "estimated_cost": None,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "generation_usage_is_synthetic": True,
        "runs": [],
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
                release_id=candidate["release_id"],
                manifest_sha256=candidate["manifest_sha256"],
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        case = next(
            c
            for c in json.loads(Path("evals/investigation-v0.1/cases.json").read_bytes())["cases"]
            if c["case_id"] == "DEV-PRIUS-JOINT"
        )
        config = FixedConfig(
            retrieval_method="hybrid",
            max_model_calls=6,
            max_tool_calls=8,
            prompt_characters=32000,
            context_mode="structured",
        )
        control = TaskControl(
            PublishedService(catalog, root / "traces"),
            HistoryModel("old", case["sources"][0]),
            root / "runs",
            config=config,
        )
        control.memory.project(
            "synthetic-user",
            ProjectInput(project_id="history-demo", name="SYNTHETIC history demonstration"),
        )
        dispatcher = Dispatcher(control)
        for task_id, mode, year in [
            ("OLD", "old", 2010),
            ("OTHER-YEAR", "old", 2011),
            ("FAILED", "failed", 2010),
            ("NEW", "new", 2010),
        ]:
            model = HistoryModel(mode, case["sources"][0])
            control.model = model
            body = {
                "release_id": candidate["release_id"],
                "allowed_source_ids": case["sources"],
                "scope": case["scope"] | {"model_year": year},
                "request": case["request"] | {"task_id": task_id},
                "mode": "EXECUTE",
                "investigator": "single_agent",
                "project_id": "history-demo",
            }
            if mode == "new":
                body["history"] = {"project_id": "history-demo"}
            ready = control.submit(
                "synthetic-user", Submission(task_id=task_id, plan=Plan.model_validate(body))
            )
            job = dispatcher.enqueue(
                "synthetic-user",
                task_id,
                Enqueue(expected_version=ready["version"], idempotency_key="history-demo"),
            )
            done = Worker(dispatcher, "history-demo").run_once(job_id=job["job_id"])
            expected = "FAILED" if mode == "failed" else "COMPLETED"
            assert done["task"]["state"] == expected, done["task"]["state"]
            with catalog.transaction() as connection:
                execution = (
                    connection.execute(sa.select(executions).where(executions.c.task_id == task_id))
                    .mappings()
                    .one()
                )
                ref = execution["private_report"]
            from runtime.artifacts import ArtifactStore

            report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                ref["report_artifact"]["sha256"]
            )
            result["runs"].append(
                {
                    "task_id": task_id,
                    "task_state": expected,
                    "disposition": (report.get("proposal") or {}).get("disposition"),
                    "error_code": report.get("error_code"),
                    "model_calls": report["model_calls"],
                    "tool_calls": len(report["tool_calls"]),
                    "elapsed_ms": report["total_elapsed_ms"],
                    "resources": report["resources"],
                    "history_references": len(
                        report.get("history_usage", {}).get("references", [])
                    ),
                    "report_reference": ref,
                    "report_content_sha256": content_hash(report),
                }
            )
            if mode == "new":
                assert report["history_usage"]["references"]
                assert all(
                    e["task_id"] != "OTHER-YEAR"
                    for e in model.inputs[0]["history_context"]["summaries"]
                )
                assert report["evidence_verification"]["status"] == "verified"
                assert {t["tool"] for t in report["tool_calls"]} >= {
                    "history_search",
                    "history_window",
                    "lookup",
                    "search",
                    "expand",
                }
        query = HistoryQuery(
            project_id="history-demo",
            release_id=candidate["release_id"],
            allowed_source_ids=case["sources"],
            scope=case["scope"],
            query="brake",
        )
        entries = control.episodes.search("synthetic-user", query.model_copy(update={"query": ""}))[
            "entries"
        ]
        assert {e["task_id"] for e in entries} == {"OLD", "FAILED", "NEW"}
        old = next(e for e in entries if e["task_id"] == "OLD")
        assert old["negative_hypotheses"] and old["counterevidence_count"]
        anchor = next(e for e in old["anchors"] if e["kind"] == "hypothesis")
        window = control.episodes.window(
            "synthetic-user",
            Window.model_validate(
                query.model_dump(mode="json")
                | {"episode_id": old["episode_id"], "event_id": anchor["event_id"]}
            ),
        )
        evidence = next(e for e in old["historical_source_handles"] if e["kind"] == "text")
        page = control.episodes.source_page(
            "synthetic-user",
            SourcePage.model_validate(
                query.model_dump(mode="json")
                | {
                    "episode_id": old["episode_id"],
                    "evidence_id": evidence["evidence_id"],
                    "page_size": 80,
                }
            ),
        )
        with engine.connect() as connection:
            result["episodes"] = connection.scalar(
                sa.select(sa.func.count()).select_from(db.episodes)
            )
            result["adoption_records"] = connection.scalar(
                sa.select(sa.func.count()).select_from(db.adoptions)
            )
        result |= {
            "release_id": candidate["release_id"],
            "case_id": case["case_id"],
            "profile": config.model_dump(mode="json"),
            "wrong_year_excluded": True,
            "failed_task_retained": True,
            "negative_hypothesis_retained_as_proposal": True,
            "window_event_count": len(window["events"]),
            "historical_source_verification": page["verification"],
            "history_is_navigation_only": True,
            "schema_removed": False,
            "root": root.as_posix(),
        }
    except Exception as error:
        result["failure_type"] = type(error).__name__
        result["failure_message"] = str(error)[-500:]
        raise
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
            {"output": args.output.as_posix(), "new_api_requests": 0, "schema_removed": True}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

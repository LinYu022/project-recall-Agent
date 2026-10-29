"""Replay frozen official records through a bounded Skill; all generation is scripted."""

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
from control.service import TaskControl
from ingestion.records import content_hash
from investigation.contracts import FixedConfig
from memory.contracts import ProjectInput
from memory.episode_contracts import Capture
from memory.procedure_contracts import (
    Candidate,
    Procedure,
    SkillSelection,
    Transition,
    ValidateProcedure,
    compile_procedure,
    missing_preconditions,
)
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService
from runtime.artifacts import ArtifactStore


class ScriptedScopeModel:
    identity = {"model_id": "scripted-procedure-comparison", "is_simulated": True}

    def __init__(self):
        self.inputs = []

    def generate(self, system, payload, *, max_tokens):
        self.inputs.append({"system": system, "payload": payload})
        proposal = {
            "disposition": "insufficient_evidence",
            "claims": [],
            "unresolved_fields": ["individual_vehicle", "causality"],
        }
        if "available_tools" in payload:
            changes = {}
            if "apply_skill" in payload["available_tools"]:
                action = "apply_skill"
            elif not payload["candidates"] and not any(
                e["kind"] == "text" for e in payload["evidence"]
            ):
                action, changes = "search", {"query": payload["initial_query"]}
            elif not any(e["kind"] == "text" for e in payload["evidence"]):
                action, changes = (
                    "expand",
                    {
                        "document_id": payload["candidates"][0]["id"],
                        "include_related": False,
                    },
                )
            else:
                action, changes = "finish", {"proposal": proposal}
            proposal = {
                "action": action,
                "reason": "SYNTHETIC mechanism check; no semantic quality judgement",
                "hypotheses": [],
                "evidence_gaps": ["individual applicability is unconfirmed"],
                "next_steps": [],
                "query": None,
                "document_id": None,
                "proposal": None,
            } | changes
        return {
            "identity": self.identity,
            "accepted_complete": True,
            "proposal": proposal,
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "elapsed_ms": 0,
            "estimated_cost": None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".local/P1-15/official-procedures.json")
    )
    args = parser.parse_args()
    schema = "procedures_" + uuid4().hex
    root = Path(".local/P1-15") / ("demo-" + uuid4().hex[:8])
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    result = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_postgresql_official_frozen_sources_and_tools_scripted_generation",
        "new_api_requests": 0,
        "new_source_requests": 0,
        "actual_provider_usage": None,
        "estimated_cost": None,
        "formal_semantic_metrics": None,
        "independent_human_reviews": 0,
        "runs": [],
        "root": root.as_posix(),
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
        bundle = json.loads(Path("examples/release-candidates.json").read_bytes())["candidates"][
            "expanded"
        ]
        catalog = Catalog(engine, BundleStore(Path(".local/releases")))
        catalog.publish(
            PublishRequest(
                release_id=bundle["release_id"],
                manifest_sha256=bundle["manifest_sha256"],
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        config = FixedConfig(
            retrieval_method="hybrid",
            retrieve_k=4,
            expand_k=1,
            max_tool_calls=6,
            max_model_calls=4,
            context_mode="structured",
            prompt_characters=32000,
        )
        control = TaskControl(
            PublishedService(catalog, root / "traces"),
            ScriptedScopeModel(),
            root / "runs",
            config=config,
        )
        owner, project = "synthetic-user", "procedure-demo"
        control.memory.project(owner, ProjectInput(project_id=project, name="SYNTHETIC Skill demo"))
        scopes = [
            {
                "make": "TOYOTA",
                "model": "PRIUS",
                "model_year": 2010,
                "campaign_number": "10V039000",
            },
            {"make": "FORD", "model": "F-250", "model_year": 2017, "campaign_number": "17V331000"},
            {
                "make": "TESLA",
                "model": "MODEL 3",
                "model_year": 2018,
                "campaign_number": "23V838000",
            },
        ]

        def plan(task_id, scope, **changes):
            return Plan.model_validate(
                {
                    "release_id": bundle["release_id"],
                    "allowed_source_ids": ["NHTSA-API-CAMPAIGN-" + scope["campaign_number"]],
                    "scope": scope,
                    "request": {
                        "task_id": task_id,
                        "intent": "record_lookup",
                        "question": "Inspect the allowed official recall scope; preserve unknowns.",
                        "retrieval_query": scope["campaign_number"],
                    },
                    "project_id": project,
                    "mode": "EXECUTE",
                }
                | changes
            )

        def run(task_id, body):
            model = ScriptedScopeModel()
            control.model = model
            ready = control.submit(owner, Submission(task_id=task_id, plan=body))
            done = control.execute(owner, task_id, ready["version"])
            with catalog.transaction() as connection:
                reference, report = control._published_report(connection, owner, task_id)
            saved = {
                "task_id": task_id,
                "task_state": done["state"],
                "disposition": (report.get("proposal") or {}).get("disposition"),
                "model_calls": report["model_calls"],
                "termination": report.get("termination"),
                "tool_calls": [t["tool"] for t in report["tool_calls"]],
                "elapsed_ms": report["total_elapsed_ms"],
                "resources": report["resources"],
                "model_input_characters_including_system": sum(
                    len(p["system"]) + len(json.dumps(p["payload"], ensure_ascii=False))
                    for p in model.inputs
                ),
                "skill_usage": report.get("skill_usage"),
                "report_reference": reference,
                "report_content_sha256": content_hash(report),
            }
            result["runs"].append(saved)
            assert done["state"] == "COMPLETED", done["state"]
            assert report["evidence_verification"]["status"] == "verified"
            return saved

        origins = []
        for task_id, scope in zip(["ORIGIN-PRIUS", "ORIGIN-FORD"], scopes[:2], strict=True):
            run(task_id, plan(task_id, scope))
            origins.append(
                control.episodes.capture(owner, Capture(task_id=task_id, project_id=project))[
                    "episode_id"
                ]
            )
        definition = Procedure(
            release_ids=[bundle["release_id"]], expires_at=datetime.now(UTC) + timedelta(days=7)
        )
        version = control.procedures.candidate(
            owner,
            Candidate(
                project_id=project,
                skill_key="inspect-scope",
                origin_episode_ids=origins,
                definition=definition,
            ),
        )
        control.submit(
            owner,
            Submission(task_id="HELDOUT-TESLA", plan=plan("HELDOUT-TESLA", scopes[2], mode="PLAN")),
        )
        check = control.procedures.validate(
            owner,
            version["version_id"],
            ValidateProcedure(
                expected_revision=1,
                definition_sha256=version["definition_sha256"],
                target_task_ids=["HELDOUT-TESLA"],
                idempotency_key="official-heldout",
            ),
        )
        result["validation"] = check
        assert check["state"] == "passed" and check["tool_calls"] == 3, check
        pointer = check["proof"]
        proof = ArtifactStore(Path(pointer["artifact_root"])).read_json(
            pointer["artifact"]["sha256"]
        )
        result["origin_event_groups"] = [o["event_groups"] for o in proof["origins"]]
        result["heldout_event_groups"] = proof["transfers"][0]["event_groups"]
        result["validation_new_model_calls"] = proof["new_model_calls"]
        current = control.procedures.get(owner, version["version_id"])
        enabled = control.procedures.transition(
            owner,
            version["version_id"],
            Transition(
                expected_revision=current["revision"],
                expected_head_revision=current["head"]["revision"],
                action="enable",
                note="SYNTHETIC reviewer; bounded official-source mechanism demonstration",
            ),
        )
        text = run(
            "TEXT-TESLA",
            plan(
                "TEXT-TESLA",
                scopes[2],
                investigator="single_agent",
                steps=[
                    "Use current lookup, then search the current query and expand one allowed matched document; "
                    "replay original evidence, preserve scope and unknown individual applicability.",
                ],
            ),
        )
        skill = run(
            "SKILL-TESLA",
            plan(
                "SKILL-TESLA",
                scopes[2],
                investigator="single_agent",
                skill=SkillSelection(
                    project_id=project,
                    version_id=enabled["version_id"],
                ),
            ),
        )
        assert text["tool_calls"] == skill["tool_calls"] == ["lookup", "search", "expand"]
        assert text["model_calls"] == 3 and skill["model_calls"] == 2
        assert text["termination"] == skill["termination"] == "agent_finished"
        assert skill["skill_usage"]["mode"] == "applied"
        missing_scope = plan("PREDICATE-ONLY", scopes[2]).scope.model_copy(
            update={"model_year": None}
        )
        request = plan("PREDICATE-ONLY", scopes[2]).request
        unsafe = definition.model_copy(update={"required_fields": ["make", "model"]})
        result["deleted_precondition_simulation"] = {
            "classification": "predicate_mutation_only_no_tools_no_model",
            "safe_missing": missing_preconditions(
                definition, missing_scope, request, bundle["release_id"]
            ),
            "unsafe_missing": missing_preconditions(
                unsafe, missing_scope, request, bundle["release_id"]
            ),
        }
        try:
            compile_procedure(unsafe)
        except ValueError:
            result["deleted_precondition_simulation"]["compiler_blocked"] = True
        else:
            raise AssertionError("unsafe procedure compiled")
        stopped = control.procedures.transition(
            owner,
            enabled["version_id"],
            Transition(
                expected_revision=enabled["revision"],
                expected_head_revision=enabled["head"]["revision"],
                action="revoke",
                note="SYNTHETIC exercise: retire version after guarded mutation",
            ),
        )
        result |= {
            "release_id": bundle["release_id"],
            "profile": config.model_dump(mode="json"),
            "final_skill_state": stopped["state"],
            "post_revoke_report_audit": control.report(owner, "SKILL-TESLA")["skill_audit"],
            "comparison_limit": "scripted action counts only; no real model quality cost latency or generalization benefit claim",
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

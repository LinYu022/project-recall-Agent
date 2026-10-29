"""One frozen cell uses the ordinary task service, worker and shared root budget."""

from pathlib import Path
from time import perf_counter
from typing import Any

import sqlalchemy as sa

from control.contracts import Plan, Submission
from control.dispatch import Dispatcher
from control.queue_contracts import Enqueue
from control.schema import executions
from control.service import TaskControl
from control.worker import Worker
from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from investigation.review import export_blind
from investigation.scoring import score
from memory.contracts import CandidateInput, ProjectInput, Review, SourceInput
from memory.episode_contracts import Capture
from memory.procedure_contracts import Candidate, Procedure, Transition, ValidateProcedure
from releases.service import PublishedService
from runtime.artifacts import ArtifactStore


class ScriptedModel:
    identity = {"model_id": "evaluation-scripted-abstention/0.1", "is_simulated": True}

    def generate(self, system, payload, *, max_tokens):
        proposal = {
            "disposition": "insufficient_evidence",
            "claims": [],
            "unresolved_fields": ["individual_vehicle", "causality", "source_coverage"],
        }
        if "available_tools" in payload:
            tools, action = payload["available_tools"], "finish"
            changes: dict[str, Any] = {}
            text = any(e["kind"] == "text" for e in payload["evidence"])
            if "apply_skill" in tools:
                action = "apply_skill"
            elif payload["remaining_model_calls"] > 1:
                if "batch" in tools and not any(
                    e["kind"] == "relationship" for e in payload["evidence"]
                ):
                    action, changes = (
                        "batch",
                        {
                            "batch": [
                                {"action": "search", "query": payload["initial_query"]},
                                {"action": "links"},
                            ]
                        },
                    )
                elif "links" in tools and not any(
                    e["kind"] == "relationship" for e in payload["evidence"]
                ):
                    action = "links"
                elif "search" in tools and not payload["candidates"] and not text:
                    action, changes = "search", {"query": payload["initial_query"]}
                elif "expand" in tools and not text:
                    action, changes = (
                        "expand",
                        {"document_id": payload["candidates"][0]["id"], "include_related": False},
                    )
            if action == "finish":
                changes = {"proposal": proposal}
            proposal = {
                "action": action,
                "reason": "SYNTHETIC engineering exercise, not semantic scoring",
                "hypotheses": [],
                "evidence_gaps": ["individual conditions remain unknown"],
                "next_steps": [],
                "query": None,
                "document_id": None,
                "proposal": None,
            } | changes
        return {
            "identity": self.identity,
            "proposal": proposal,
            "accepted_complete": True,
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "estimated_cost": None,
            "elapsed_ms": 0,
        }


def plan_for(protocol, case, task_id, arm, *, project=None, skill=None):
    body = {
        "release_id": protocol.dataset_release_id,
        "allowed_source_ids": case.sources,
        "scope": case.scope.model_dump(mode="json"),
        "request": case.request.model_dump(mode="json") | {"task_id": task_id},
        "mode": "EXECUTE",
        "investigator": arm.method,
        "project_id": project,
    }
    if arm.memory != "none":
        body["memory"] = {"project_id": project}
    if arm.memory in {"semantic_episode", "all"}:
        body["history"] = {"project_id": project}
    if arm.memory == "all":
        body["skill"] = {"project_id": project, "version_id": skill["version_id"]}
    return Plan.model_validate(body)


def execute(control, owner, task_id, plan):
    ready = control.submit(owner, Submission(task_id=task_id, plan=plan))
    if ready["state"] != "DRAFT":
        return ready, None
    dispatch = control.dispatcher or Dispatcher(control)
    job = dispatch.enqueue(
        owner,
        task_id,
        Enqueue(expected_version=ready["version"], idempotency_key="frozen-evaluation"),
    )
    done = Worker(dispatch, "evaluation-worker").run_once(job_id=job["job_id"])
    return done["task"], done["calls"]


def memory_prefix(control, protocol, prefix, case, owner, project, *, enable_skill=False):
    """Reset by constructing a fresh owner/project; every arm installs the same inactive baseline."""
    from datetime import UTC, datetime, timedelta

    control.memory.project(
        owner, ProjectInput(project_id=project, name="SYNTHETIC evaluation prefix")
    )
    for declaration in prefix["declarations"]:
        source = control.memory.source(
            owner,
            SourceInput(
                project_id=project,
                origin="user_statement",
                text="SYNTHETIC evaluation declaration; no target answers or scoring labels",
            ),
        )
        candidate = control.memory.candidate(
            owner,
            CandidateInput(
                source_id=source["source_id"], proposal=declaration | {"project_id": project}
            ),
        )
        control.memory.review(
            owner,
            candidate["candidate_id"],
            Review(
                expected_revision=candidate["revision"],
                proposal_sha256=candidate["proposal_sha256"],
                decision="approve",
                note="SYNTHETIC prefix installation, not a human quality review",
            ),
        )
    original = control.model
    control.model = ScriptedModel()
    origins, observed = [], []
    try:
        for i, scope in enumerate([*prefix["origin_scopes"], case.scope.model_dump(mode="json")]):
            task_id = f"PX-{owner[-12:].upper()}-{i}"
            # The target prefix only inspects source scope. It has no target question, claims or labels.
            source_id = (
                prefix.get(
                    "origin_source_ids",
                    ["NHTSA-API-CAMPAIGN-" + s["campaign_number"] for s in prefix["origin_scopes"]],
                )[i]
                if i < 2
                else case.sources[0]
            )
            body = Plan.model_validate(
                {
                    "release_id": protocol.dataset_release_id,
                    "allowed_source_ids": [source_id],
                    "scope": scope,
                    "request": {
                        "task_id": task_id,
                        "intent": "record_lookup",
                        "question": "SYNTHETIC prefix: inspect the allowed source scope only; no target answer.",
                        "retrieval_query": scope["campaign_number"],
                    },
                    "mode": "EXECUTE",
                    "investigator": "fixed",
                    "project_id": project,
                }
            )
            state, calls = execute(control, owner, task_id, body)
            if state["state"] != "COMPLETED":
                raise ValueError("frozen_prefix_flow_failed")
            episode = control.episodes.capture(owner, Capture(task_id=task_id, project_id=project))
            if i < 2:
                origins.append(episode["episode_id"])
            observed.append(
                {"task_id": task_id, "report": state["published_report"], "calls": calls}
            )
        if not enable_skill:
            return {
                "skill": None,
                "flows": observed,
                "validation": None,
                "prefix_spec_sha256": content_hash(prefix),
                "provider_calls": 0,
                "scope": case.scope.model_dump(mode="json"),
                "target_question_or_labels_used": False,
            }
        definition = Procedure(
            release_ids=[protocol.dataset_release_id],
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        version = control.procedures.candidate(
            owner,
            Candidate(
                project_id=project,
                skill_key="scope-check",
                origin_episode_ids=origins,
                definition=definition,
            ),
        )
        task_id = "PV-" + owner[-12:].upper()
        target = body.model_copy(deep=True)
        target.mode = "PLAN"
        target.request = target.request.model_copy(update={"task_id": task_id})
        control.submit(owner, Submission(task_id=task_id, plan=target))
        validation = control.procedures.validate(
            owner,
            version["version_id"],
            ValidateProcedure(
                expected_revision=version["revision"],
                definition_sha256=version["definition_sha256"],
                target_task_ids=[task_id],
                idempotency_key="prefix-validation",
            ),
        )
        if validation["state"] != "passed":
            proof = ArtifactStore(control.root / "prefix").put_json(
                {"flows": observed, "validation": validation}
            )
            raise ValueError("frozen_prefix_procedure_validation_failed:" + proof["sha256"])
        current = control.procedures.get(owner, version["version_id"])
        enabled = control.procedures.transition(
            owner,
            version["version_id"],
            Transition(
                expected_revision=current["revision"],
                expected_head_revision=current["head"]["revision"],
                action="enable",
                note="SYNTHETIC frozen evaluation prefix",
            ),
        )
        return {
            "skill": enabled,
            "flows": observed,
            "validation": validation,
            "prefix_spec_sha256": content_hash(prefix),
            "provider_calls": 0,
            "scope": case.scope.model_dump(mode="json"),
            "target_question_or_labels_used": False,
        }
    finally:
        control.model = original


class CellExecutor:
    def __init__(
        self,
        catalog,
        root,
        protocol,
        prefix,
        *,
        model_factory,
        label_root,
        service_factory=PublishedService,
    ):
        self.catalog, self.root, self.protocol, self.prefix = catalog, root, protocol, prefix
        self.model_factory, self.label_root = model_factory, label_root
        self.service_factory = service_factory
        self.sequences = {}

    def run(self, cell, *, inject_setup=None):
        tick = perf_counter()
        case = next(c for c in self.protocol.cases if c.case_id == cell["case_id"])
        arm = next(a for a in self.protocol.arms if a.arm_id == cell["arm_id"])
        config = self.protocol.profile.model_copy(
            update={"retrieval_method": arm.retrieval, "context_mode": arm.context}
        )
        sequence_key = (arm.arm_id, cell["repeat"])
        if arm.family == "memory_sequential" and sequence_key in self.sequences:
            control, owner, project, prefix_state = self.sequences[sequence_key]
            control.model = self.model_factory(cell)
            setup_reused = True
        else:
            run_key = content_hash(cell)[:12]
            owner, project = "evaluation-" + run_key, "memory-eval"
            control = TaskControl(
                self.service_factory(self.catalog, self.root / "traces"),
                self.model_factory(cell),
                self.root / run_key,
                config=config,
            )
            prefix_state, setup_reused = None, False
        task_id = "EVAL-" + content_hash(cell)[:24].upper()
        observation = {
            "state": "failed",
            "category": case.category,
            "generation_mode": "scripted"
            if control.model.identity.get("is_simulated")
            else "live_provider",
            "error_code": None,
            "end_to_end_ms": None,
            "human_wait_ms": 0,
            "formal_task_success": None,
            "provider_cost": None,
            "evidence_verified": None,
            "prefix_reused": setup_reused,
        }
        try:
            if arm.family.startswith("memory") and not setup_reused:
                prefix_state = (inject_setup or memory_prefix)(
                    control,
                    self.protocol,
                    self.prefix,
                    case,
                    owner,
                    project,
                    enable_skill=arm.memory == "all",
                )
            if arm.family == "memory_sequential":
                self.sequences[sequence_key] = (control, owner, project, prefix_state)
            observation["prefix_preparation"] = prefix_state
            observation["setup_ms"] = (perf_counter() - tick) * 1000
            plan = plan_for(
                self.protocol,
                case,
                task_id,
                arm,
                project=project if arm.family.startswith("memory") else None,
                skill=prefix_state["skill"] if prefix_state else None,
            )
            task, calls = execute(control, owner, task_id, plan)
            observation |= {
                "task_state": task["state"],
                "calls": calls,
                "frozen_scope": plan.scope.model_dump(mode="json"),
                "allowed_source_ids": plan.allowed_source_ids,
                "effective_config": config.model_dump(mode="json"),
            }
            if task["state"] in {"WAITING_INPUT", "WAITING_APPROVAL"}:
                observation |= {"state": "guarded", "guard": task["questions"]}
            with self.catalog.transaction() as connection:
                execution = (
                    connection.execute(
                        sa.select(executions)
                        .where(executions.c.task_id == task_id)
                        .order_by(executions.c.epoch.desc())
                    )
                    .mappings()
                    .first()
                )
                if execution and execution["private_report"]:
                    pointer = execution["private_report"]
                    report = ArtifactStore(Path(pointer["artifact_root"])).read_json(
                        pointer["report_artifact"]["sha256"]
                    )
                    reference = ReferenceCase.model_validate_json(
                        (self.label_root / "references" / (case.case_id + ".json")).read_bytes()
                    )
                    if content_hash(reference.model_dump(mode="json")) != case.reference_sha256:
                        raise ValueError("frozen_reference_changed")
                    observation |= {
                        "state": "completed" if task["state"] == "COMPLETED" else "failed",
                        "report_reference": pointer,
                        "report_content_sha256": content_hash(report),
                        "evidence_verified": (report.get("evidence_verification") or {}).get(
                            "status"
                        )
                        == "verified",
                        "score": score(report, reference),
                        "report_summary": {
                            k: report.get(k)
                            for k in [
                                "execution_status",
                                "termination",
                                "error_code",
                                "proposal",
                                "model_calls",
                                "resources",
                                "delegation_policy",
                                "branch_results",
                                "branch_timing",
                                "history_usage",
                                "skill_usage",
                                "memory_usage",
                            ]
                        },
                        "blind_id": export_blind(report, self.root / "blind"),
                        "tool_calls": len(report["tool_calls"]),
                    }
        except Exception as error:
            import traceback

            observation |= {
                "state": "failed",
                "error_code": type(error).__name__ + ":" + str(error)[:200],
                "failure_artifact": ArtifactStore(self.root / "failures").put_json(
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                        "traceback": traceback.format_exc(),
                    }
                ),
            }
        observation["end_to_end_ms"] = (perf_counter() - tick) * 1000
        if "blind_id" not in observation:
            from evaluation.review import export_guard

            observation["blind_id"] = export_guard(observation, case, self.root / "blind")
        return observation

"""Procedure candidates need multiple independently replayed origins and real transfer execution."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from control.access import (
    Access,
    AccessDenied,
    ControlConflict,
    checked_grant,
    checked_revision,
    row,
    using,
    validate,
)
from control.contracts import Plan, active_grant, within
from ingestion.records import content_hash
from investigation.context import FrozenContext
from investigation.contracts import EvidenceCard, FixedConfig
from investigation.evidence import verify_cards
from memory import episode_schema as episodes
from memory import procedure_schema as db
from memory.procedure_contracts import Procedure, compile_procedure
from memory.procedure_engine import event_groups, execute_scope_recipe
from memory.service import now
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def engine_fingerprint():
    import hashlib

    return content_hash(
        {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                Path("memory/procedure_engine.py"),
                Path("memory/procedure_contracts.py"),
                Path("memory/procedures.py"),
                Path("memory/procedure_runtime.py"),
                Path("examples/procedure-event-families.json"),
                Path("control/contracts.py"),
                Path("investigation/evidence.py"),
            ]
        }
    )


class ProcedureService:
    def __init__(self, control):
        self.control, self.catalog = control, control.catalog
        self.memory, self.episodes = control.memory, control.episodes

    def _version(self, connection, owner, version_id):
        value = self.memory._owned(connection, db.versions, "version_id", version_id, owner)
        if content_hash(value["definition"]) != value["definition_sha256"]:
            raise ArtifactIntegrityError("procedure_definition_changed")
        Procedure.model_validate(value["definition"])
        return value

    def _definition(self, version, clock):
        definition = Procedure.model_validate(version["definition"])
        compile_procedure(definition)
        if definition.expires_at <= clock:
            raise AccessDenied("procedure_version_expired")
        return definition

    def _origin(self, connection, owner, episode_id, *, replay):
        episode = self.memory._owned(connection, episodes.episodes, "episode_id", episode_id, owner)
        self.episodes._project(connection, owner, episode["project_id"])
        body = self.episodes._body(episode)
        self.episodes._source(connection, owner, body)
        if not episode["enabled"]:
            raise AccessDenied("procedure_origin_disabled")
        if body["task_state"] != "COMPLETED" or not body["reports"]:
            raise ValueError("procedure_origin_not_a_completed_verified_flow")
        ref = body["reports"][-1]
        store = ArtifactStore(Path(ref["artifact_root"]))
        report = store.read_json(ref["report_artifact"]["sha256"])
        observed = [t["tool"] for t in report["tool_calls"] if t["status"] == "completed"]
        if (
            report["execution_status"] != "completed"
            or report.get("termination") not in {None, "agent_finished"}
            or not all(t in observed for t in ("lookup", "search", "expand"))
            or (report.get("proposal") or {}).get("disposition")
            not in {"answered", "insufficient_evidence"}
            or (report.get("evidence_verification") or {}).get("status") != "verified"
        ):
            raise ValueError("procedure_origin_has_not_met_bounded_flow_contract")
        campaigns = event_groups(report["cards"])
        if not campaigns:
            raise ValueError("procedure_origin_event_identity_unknown")
        if replay:
            _, manifest = self.catalog._available(connection, body["release_id"])
            revision = checked_revision(connection, body["revision_id"])
            context = self.episodes._historical_context(connection, revision, manifest)
            verify_cards(context, store, [EvidenceCard.model_validate(c) for c in report["cards"]])
        return {
            "episode_id": episode_id,
            "episode_revision": episode["revision"],
            "snapshot_sha256": episode["body_sha256"],
            "task_id": body["task_id"],
            "project_id": episode["project_id"],
            "release_id": body["release_id"],
            "event_groups": campaigns,
            "report_reference": ref,
            "success_basis": "bounded_lookup_search_expand_and_original_replay_not_semantic_quality",
        }

    def candidate(self, owner, value):
        if len(set(value.origin_episode_ids)) != len(value.origin_episode_ids):
            raise ValueError("procedure_origin_ids_must_be_unique")
        with self.catalog.transaction() as connection:
            self.episodes._project(connection, owner, value.project_id)
            origins = [
                self._origin(connection, owner, e, replay=True) for e in value.origin_episode_ids
            ]
            if any(p["project_id"] != value.project_id for p in origins):
                raise AccessDenied("procedure_origins_outside_project")
            definition = value.definition or Procedure(
                release_ids=sorted({p["release_id"] for p in origins}),
                expires_at=now(connection) + timedelta(days=30),
            )
            number = 1 + (
                connection.scalar(
                    sa.select(sa.func.max(db.versions.c.version_number)).where(
                        db.versions.c.owner_id == owner,
                        db.versions.c.project_id == value.project_id,
                        db.versions.c.skill_key == value.skill_key,
                    )
                )
                or 0
            )
            record = {
                "version_id": "SKV-" + uuid4().hex,
                "owner_id": owner,
                "project_id": value.project_id,
                "skill_key": value.skill_key,
                "version_number": number,
                "revision": 1,
                "state": "candidate",
                "definition": definition.model_dump(mode="json"),
                "definition_sha256": content_hash(definition.model_dump(mode="json")),
                "origins": origins,
                "validation_id": None,
            }
            connection.execute(db.versions.insert().values(**record))
            connection.execute(
                insert(db.heads)
                .values(
                    owner_id=owner,
                    project_id=value.project_id,
                    skill_key=value.skill_key,
                    revision=1,
                    version_id=None,
                )
                .on_conflict_do_nothing()
            )
            self.memory._event(
                connection,
                owner,
                "procedure_candidate_created",
                {
                    "version_id": record["version_id"],
                    "definition_sha256": record["definition_sha256"],
                    "origin_count": len(origins),
                },
            )
            return record

    def get(self, owner, version_id):
        with self.catalog.transaction() as connection:
            version = self._version(connection, owner, version_id)
            head = (
                connection.execute(
                    sa.select(db.heads).where(
                        db.heads.c.owner_id == owner,
                        db.heads.c.project_id == version["project_id"],
                        db.heads.c.skill_key == version["skill_key"],
                    )
                )
                .mappings()
                .one()
            )
            return version | {"head": dict(head)}

    def _origins(self, connection, owner, version, *, replay):
        proofs = [
            self._origin(connection, owner, p["episode_id"], replay=replay)
            for p in version["origins"]
        ]
        if proofs != version["origins"]:
            raise AccessDenied("procedure_origin_changed_or_revoked")
        return proofs

    def _research(self, connection, owner, task_id):
        task = self.control._task(connection, task_id, owner)
        access = Access(task_id, task["revision_id"], task["grant_id"], task["epoch"])
        _, revision, _ = validate(connection, access, "research", now(connection))
        plan = Plan.model_validate(revision["body"])
        if task["state"] != "DRAFT" or plan.mode != "PLAN":
            raise AccessDenied("procedure_validation_requires_draft_plan_research_tasks")
        from control.queue_schema import pools

        ancestor = task
        while ancestor["parent_id"]:
            ancestor = self.control._task(connection, ancestor["parent_id"], owner)
        if row(connection, pools, "root_task_id", ancestor["task_id"]):
            raise AccessDenied("procedure_validation_cannot_bypass_dispatched_budget")
        return task, revision, plan, access

    def _reserve(self, validation_id, owner, pinned_revision):
        with self.catalog.transaction() as connection:
            validation = self.memory._owned(
                connection, db.validations, "validation_id", validation_id, owner
            )
            version = self._version(connection, owner, validation["version_id"])
            self.episodes._project(connection, owner, version["project_id"])
            self._definition(version, now(connection))
            if (
                version["revision"] != pinned_revision
                or version["state"] != "candidate"
                or validation["state"] != "running"
                or validation["tool_calls"] >= validation["tool_limit"]
            ):
                raise AccessDenied("procedure_validation_stopped_or_budget_exhausted")
            self._origins(connection, owner, version, replay=False)
            connection.execute(
                db.validations.update()
                .where(db.validations.c.validation_id == validation_id)
                .values(tool_calls=validation["tool_calls"] + 1)
            )

    def validate(self, owner, version_id, request):
        fingerprint = content_hash(request.model_dump(mode="json"))
        validation_id = "SKT-" + uuid4().hex
        with self.catalog.transaction() as connection:
            version = self._version(connection, owner, version_id)
            previous = (
                connection.execute(
                    sa.select(db.validations).where(
                        db.validations.c.owner_id == owner,
                        db.validations.c.version_id == version_id,
                        db.validations.c.idempotency_key == request.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["input_sha256"] != fingerprint:
                    raise ControlConflict("procedure_validation_key_reused_for_different_input")
                return dict(previous)
            if (
                version["revision"] != request.expected_revision
                or version["definition_sha256"] != request.definition_sha256
                or version["state"] != "candidate"
            ):
                raise ControlConflict("procedure_candidate_changed")
            connection.execute(
                db.validations.insert().values(
                    validation_id=validation_id,
                    version_id=version_id,
                    owner_id=owner,
                    idempotency_key=request.idempotency_key,
                    input_sha256=fingerprint,
                    state="running",
                    tool_calls=0,
                    tool_limit=3 * len(request.target_task_ids),
                    proof=None,
                )
            )
        proof = {
            "definition_sha256": version["definition_sha256"],
            "engine_sha256": engine_fingerprint(),
            "classification": "real_readonly_recipe_execution_and_original_replay_no_generation",
            "transfers": [],
            "new_model_calls": 0,
            "failure_code": None,
        }
        try:
            with self.catalog.transaction() as connection:
                definition = self._definition(version, now(connection))
                origins = self._origins(connection, owner, version, replay=True)
                groups = [set(p["event_groups"]) for p in origins]
                if len(origins) < 2 or any(
                    a & b for i, a in enumerate(groups) for b in groups[i + 1 :]
                ):
                    raise ValueError("procedure_requires_multiple_independent_origin_events")
                original_tasks = {p["task_id"] for p in origins}
                training_groups = set().union(*groups)
                if (
                    len(set(request.target_task_ids)) != len(request.target_task_ids)
                    or set(request.target_task_ids) & original_tasks
                ):
                    raise ValueError("procedure_transfer_must_use_distinct_nonorigin_tasks")
            heldout_groups: set[str] = set()
            for task_id in request.target_task_ids:
                with self.catalog.transaction() as connection:
                    task, revision, plan, access = self._research(connection, owner, task_id)
                    from memory.episode_contracts import project_for

                    if project_for(plan) != version["project_id"]:
                        raise AccessDenied("procedure_transfer_project_differs")
                self.control._ensure_binding(task, revision)
                store = ArtifactStore(self.control.root / "procedures" / validation_id / task_id)
                attempt = {"task_id": task_id, "tool_calls": [], "completed": False}
                proof.setdefault("attempts", []).append(attempt)
                with using(access):
                    context = FrozenContext(self.control.service, revision["binding_task_id"])
                    calls = []

                    def invoke(tool, arguments, operation):
                        self._reserve(validation_id, owner, request.expected_revision)
                        calls.append(tool)
                        attempt["tool_calls"].append(tool)
                        return operation()

                    def different_event(campaigns):
                        if not campaigns or set(campaigns) & training_groups:
                            raise ValueError("procedure_transfer_event_overlaps_training")
                        if set(campaigns) & heldout_groups:
                            raise ValueError("procedure_transfer_events_not_independent")

                    result, cards = execute_scope_recipe(
                        self.control.service,
                        context,
                        plan.request.model_copy(update={"task_id": revision["binding_task_id"]}),
                        definition,
                        self.control.config or FixedConfig(),
                        store,
                        invoke,
                        after_lookup=different_event,
                    )
                    if result["status"] != "scope_inspected" or set(calls) != {
                        "lookup",
                        "search",
                        "expand",
                    }:
                        raise ValueError("procedure_transfer_did_not_meet_postconditions")
                    with self.catalog.transaction() as connection:
                        validate(connection, access, "research", now(connection))
                    artifact = store.put_json(
                        {"result": result, "cards": [c.model_dump(mode="json") for c in cards]}
                    )
                    proof["transfers"].append(
                        {
                            "task_id": task_id,
                            "revision_id": task["revision_id"],
                            "grant_id": task["grant_id"],
                            "event_groups": result["event_groups"],
                            "release_id": plan.release_id,
                            "result": result,
                            "tool_calls": calls,
                            "artifact_root": (store.directory.parent).as_posix(),
                            "artifact": artifact,
                        }
                    )
                    attempt["completed"] = True
                    heldout_groups.update(result["event_groups"])
            proof["origins"] = origins
            state = "passed"
        except (ValueError, OSError, AccessDenied) as error:
            state = "failed"
            proof["failure_code"] = str(error)[:300]
        with self.catalog.transaction() as connection:
            original_validation = self.memory._owned(
                connection, db.validations, "validation_id", validation_id, owner
            )
            current = self._version(connection, owner, version_id)
            if current["revision"] != request.expected_revision or current["state"] != "candidate":
                state = "failed"
                proof["failure_code"] = "procedure_candidate_changed_during_validation"
            if original_validation["state"] != "running":
                state = "failed"
                proof["failure_code"] = "procedure_validation_previously_aborted"
                proof["prior_terminal_proof"] = original_validation["proof"]
            if state == "passed":
                try:
                    self._definition(current, now(connection))
                    self._origins(connection, owner, current, replay=False)
                    for transfer in proof["transfers"]:
                        target, revision, plan, access = self._research(
                            connection, owner, transfer["task_id"]
                        )
                        if (
                            target["revision_id"] != transfer["revision_id"]
                            or target["grant_id"] != transfer["grant_id"]
                        ):
                            raise AccessDenied("procedure_transfer_changed_before_finalization")
                except (ValueError, OSError, AccessDenied) as error:
                    state = "failed"
                    proof["failure_code"] = str(error)[:300]
            proof["validation_state"] = state
            proof_ref = ArtifactStore(self.control.root / "procedures" / validation_id).put_json(
                proof
            )
            connection.execute(
                db.validations.update()
                .where(db.validations.c.validation_id == validation_id)
                .values(
                    state=state,
                    proof={
                        "artifact_root": (
                            self.control.root / "procedures" / validation_id
                        ).as_posix(),
                        "artifact": proof_ref,
                    },
                )
            )
            if state == "passed":
                connection.execute(
                    db.versions.update()
                    .where(db.versions.c.version_id == version_id)
                    .values(
                        state="validated",
                        revision=current["revision"] + 1,
                        validation_id=validation_id,
                    )
                )
            self.memory._event(
                connection,
                owner,
                "procedure_validation_" + state,
                {
                    "version_id": version_id,
                    "validation_id": validation_id,
                    "proof_sha256": proof_ref["sha256"],
                },
            )
            return dict(
                connection.execute(
                    sa.select(db.validations).where(db.validations.c.validation_id == validation_id)
                )
                .mappings()
                .one()
            )

    def validation(self, owner, validation_id):
        with self.catalog.transaction() as connection:
            return self.memory._owned(
                connection, db.validations, "validation_id", validation_id, owner
            )

    def abort_validation(self, owner, validation_id, request):
        with self.catalog.transaction() as connection:
            validation = self.memory._owned(
                connection, db.validations, "validation_id", validation_id, owner
            )
            if validation["state"] != request.expected_state:
                raise ControlConflict("validation_not_running")
            store = ArtifactStore(self.control.root / "procedures" / validation_id)
            ref = store.put_json(
                {
                    "validation_state": "failed",
                    "failure_code": "explicitly_aborted",
                    "note": request.note,
                    "inflight_readonly_operations_may_complete": True,
                }
            )
            pointer = {"artifact_root": store.directory.parent.as_posix(), "artifact": ref}
            connection.execute(
                db.validations.update()
                .where(db.validations.c.validation_id == validation_id)
                .values(state="failed", proof=pointer)
            )
            self.memory._event(
                connection,
                owner,
                "procedure_validation_aborted",
                {"validation_id": validation_id, "note": request.note},
            )
            return dict(validation) | {"state": "failed", "proof": pointer}

    def _proof(self, connection, owner, version, *, replay):
        validation = self.memory._owned(
            connection, db.validations, "validation_id", version["validation_id"], owner
        )
        if validation["state"] != "passed" or validation["version_id"] != version["version_id"]:
            raise AccessDenied("procedure_validation_not_passed")
        ref = validation["proof"]
        proof = ArtifactStore(Path(ref["artifact_root"])).read_json(ref["artifact"]["sha256"])
        if (
            proof["definition_sha256"] != version["definition_sha256"]
            or proof["engine_sha256"] != engine_fingerprint()
            or not proof["transfers"]
        ):
            raise AccessDenied("procedure_validation_stale")
        self._origins(connection, owner, version, replay=replay)
        for transfer in proof["transfers"]:
            task = self.control._task(connection, transfer["task_id"], owner)
            grant = checked_grant(connection, transfer["grant_id"])
            if (
                task["revision_id"] != transfer["revision_id"]
                or task["grant_id"] != transfer["grant_id"]
                or not active_grant(grant, now(connection))
            ):
                raise AccessDenied("procedure_transfer_authorization_changed")
            revision = checked_revision(connection, transfer["revision_id"])
            plan = Plan.model_validate(revision["body"])
            if (
                grant["task_id"] != task["task_id"]
                or not within(plan, grant["body"])
                or revision["frozen_time"] != grant["body"]["time"]
                or "research" not in grant["body"]["operations"]
            ):
                raise AccessDenied("procedure_transfer_exceeds_authorization")
            self.control._parent(connection, task, plan, delivery=True)
            artifact = ArtifactStore(Path(transfer["artifact_root"]))
            saved = artifact.read_json(transfer["artifact"]["sha256"])
            if (
                saved["result"] != transfer["result"]
                or content_hash(saved["cards"]) != saved["result"]["cards_sha256"]
            ):
                raise ArtifactIntegrityError("procedure_transfer_proof_changed")
            if replay:
                _, manifest = self.catalog._available(connection, transfer["release_id"])
                context = self.episodes._historical_context(connection, revision, manifest)
                verify_cards(
                    context, artifact, [EvidenceCard.model_validate(c) for c in saved["cards"]]
                )
        return proof

    def transition(self, owner, version_id, request):
        return self._transition(owner, version_id, request, rollback=False)

    def rollback(self, owner, version_id, request):
        if request.action != "enable":
            raise ValueError("rollback_requires_enable_action")
        return self._transition(owner, version_id, request, rollback=True)

    def _transition(self, owner, version_id, request, *, rollback):
        with self.catalog.transaction() as connection:
            version = self._version(connection, owner, version_id)
            self.episodes._project(connection, owner, version["project_id"])
            head = (
                connection.execute(
                    sa.select(db.heads).where(
                        db.heads.c.owner_id == owner,
                        db.heads.c.project_id == version["project_id"],
                        db.heads.c.skill_key == version["skill_key"],
                    )
                )
                .mappings()
                .one()
            )
            if (
                version["revision"] != request.expected_revision
                or head["revision"] != request.expected_head_revision
            ):
                raise ControlConflict("procedure_version_or_head_changed")
            if version["state"] == "revoked":
                raise ControlConflict("revoked_procedure_cannot_be_restored")
            if request.action == "enable":
                if version["state"] != ("deprecated" if rollback else "validated"):
                    raise ControlConflict("only_validated_procedure_can_be_enabled")
                self._definition(version, now(connection))
                self._proof(connection, owner, version, replay=True)
                if head["version_id"]:
                    connection.execute(
                        db.versions.update()
                        .where(db.versions.c.version_id == head["version_id"])
                        .values(state="deprecated", revision=db.versions.c.revision + 1)
                    )
                selected, state = version_id, "enabled"
            else:
                if request.action == "deprecate" and version["state"] not in {
                    "validated",
                    "enabled",
                }:
                    raise ControlConflict("only_validated_or_enabled_version_can_be_deprecated")
                selected = None if head["version_id"] == version_id else head["version_id"]
                state = "deprecated" if request.action == "deprecate" else "revoked"
            connection.execute(
                db.versions.update()
                .where(db.versions.c.version_id == version_id)
                .values(state=state, revision=version["revision"] + 1)
            )
            connection.execute(
                db.heads.update()
                .where(
                    db.heads.c.owner_id == owner,
                    db.heads.c.project_id == version["project_id"],
                    db.heads.c.skill_key == version["skill_key"],
                )
                .values(version_id=selected, revision=head["revision"] + 1)
            )
            self.memory._event(
                connection,
                owner,
                "procedure_rollback" if rollback else "procedure_" + request.action,
                {"version_id": version_id, "head_version": selected, "note": request.note},
            )
        return self.get(owner, version_id)

    def usable(self, connection, owner, selection, *, reference=None):
        version = self._version(connection, owner, selection.version_id)
        self.episodes._project(connection, owner, selection.project_id)
        definition = self._definition(version, now(connection))
        head = (
            connection.execute(
                sa.select(db.heads).where(
                    db.heads.c.owner_id == owner,
                    db.heads.c.project_id == selection.project_id,
                    db.heads.c.skill_key == version["skill_key"],
                )
            )
            .mappings()
            .first()
        )
        if (
            version["project_id"] != selection.project_id
            or version["state"] != "enabled"
            or head is None
            or head["version_id"] != version["version_id"]
        ):
            raise AccessDenied("procedure_not_currently_enabled")
        if reference and (
            version["revision"] != reference["revision"]
            or version["definition_sha256"] != reference["definition_sha256"]
        ):
            raise AccessDenied("adopted_procedure_changed")
        self._proof(connection, owner, version, replay=False)
        return version, definition

    def audit(self, connection, owner, reference):
        version = self._version(connection, owner, reference["version_id"])
        from memory.procedure_contracts import SkillSelection

        try:
            self.usable(
                connection,
                owner,
                SkillSelection(project_id=version["project_id"], version_id=version["version_id"]),
                reference=reference,
            )
            state, reason = "active", None
        except (ValueError, OSError, AccessDenied) as error:
            state, reason = "unavailable", str(error)[:300]
        return {"version_id": reference["version_id"], "state": state, "reason": reason}

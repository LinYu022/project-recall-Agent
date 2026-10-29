"""Trusted user submissions, immutable revision/grant history and atomic report visibility."""

import hashlib
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

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
from control.contracts import (
    Approval,
    AuthorizationBoundary,
    InputAnswer,
    Plan,
    Research,
    Submission,
    active_grant,
    missing_conditions,
    operations,
    transition,
    within,
)
from control.schema import authorizations, events, executions, questions, revisions, tasks
from ingestion.records import content_hash
from investigation.communication import clarification_prompt
from investigation.contracts import InvestigationRequest
from investigation.flow import FixedInvestigator
from releases.contracts import BindTask, ReleaseQuery
from retrieval.index import checked_index
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def identifier(prefix):
    return prefix + uuid4().hex.upper()


class TaskControl:
    def __init__(self, service, model, root: Path, *, config=None):
        self.service, self.catalog, self.model, self.root = service, service.catalog, model, root
        self.config = config
        self.dispatcher = None
        from memory.service import MemoryService

        self.memory = MemoryService(self.catalog)
        from memory.episodes import EpisodeService

        self.episodes = EpisodeService(self)
        from memory.procedures import ProcedureService

        self.procedures = ProcedureService(self)

    def _task(self, connection, task_id, owner):
        task = row(connection, tasks, "task_id", task_id)
        if task is None or task["owner_id"] != owner:
            raise AccessDenied("task_not_owned")
        return task

    def _history_project(self, owner, plan):
        from memory.episode_contracts import project_for

        project = project_for(plan)
        if project:
            with self.catalog.transaction() as connection:
                self.memory._project(connection, owner, project)

    def _capture_episode(self, connection, owner, task_id, plan):
        project = plan.project_id or (
            plan.history.project_id if plan.history and plan.history.enabled else None
        )
        if project:
            task = self._task(connection, task_id, owner)
            if not active_grant(checked_grant(connection, task["grant_id"]), datetime.now(UTC)):
                return
            if (
                not self.memory._project(connection, owner, project)["enabled"]
                or not self.memory._state(connection, owner)["enabled"]
            ):
                return
            try:
                self._parent(connection, task, plan, delivery=True)
            except AccessDenied:
                return
            try:
                with connection.begin_nested():
                    self.episodes._capture(connection, owner, task_id, project)
            except (ArtifactIntegrityError, OSError, ValueError) as error:
                current = self._task(connection, task_id, owner)
                self._event(
                    connection,
                    current,
                    owner,
                    "history_capture_failed",
                    changes={"quality": current["quality"] | {"history_index": "failed"}},
                    payload={"error_type": type(error).__name__},
                )

    def _version(self, task, expected):
        if task["version"] != expected:
            raise ControlConflict("task_version_changed")

    def _event(self, connection, task, actor, action, *, target=None, changes=None, payload=None):
        values = dict(changes or {})
        values["version"] = task["version"] + 1
        values["state"] = transition(task["state"], target or task["state"])
        connection.execute(
            tasks.update().where(tasks.c.task_id == task["task_id"]).values(**values)
        )
        connection.execute(
            events.insert().values(
                task_id=task["task_id"],
                version=values["version"],
                actor=actor,
                action=action,
                payload={
                    "from_state": task["state"],
                    "to_state": values["state"],
                    "revision_id": values.get("revision_id", task["revision_id"]),
                    "grant_id": values.get("grant_id", task["grant_id"]),
                    "epoch": values.get("epoch", task["epoch"]),
                    **(payload or {}),
                },
            )
        )
        return task | values

    def _plan(self, plan):
        # Pydantic model_copy is not validation; all trust-boundary inputs are revalidated.
        plan = Plan.model_validate(plan.model_dump(mode="json"))
        if len(plan.allowed_source_ids) != len(set(plan.allowed_source_ids)):
            raise ValueError("duplicate_plan_sources")
        if plan.request.expected_release_id not in {None, plan.release_id}:
            raise ValueError("request_release_differs_from_plan")
        with self.catalog.transaction(shared=True) as connection:
            _, manifest = self.catalog._available(connection, plan.release_id)
            if not set(plan.allowed_source_ids) <= set(manifest["allowed_source_ids"]):
                raise AccessDenied("source_outside_release")
            corpus = checked_index(
                self.catalog.store.directory(manifest) / "index", manifest["index_artifact_sha256"]
            )["corpus"]
            frozen_time = corpus["recipe"]["request"]["time"]
        return plan, frozen_time

    def _revision(self, connection, task, plan, frozen_time, previous=None):
        revision_id, binding = identifier("REV-"), identifier("CT-")
        body = plan.model_dump(mode="json")
        connection.execute(
            revisions.insert().values(
                revision_id=revision_id,
                task_id=task["task_id"],
                predecessor_id=previous,
                binding_task_id=binding,
                body=body,
                body_sha256=content_hash(body),
                frozen_time=frozen_time,
            )
        )
        return revision_id

    def _grant(self, connection, task, plan, frozen_time, owner, basis):
        grant_id = identifier("AUTH-")
        body = AuthorizationBoundary.model_validate(
            {
                "release_id": plan.release_id,
                "scope": plan.scope.model_dump(mode="json"),
                "allowed_source_ids": plan.allowed_source_ids,
                "operations": operations(plan.mode),
                "time": frozen_time,
                "memory": plan.memory.model_dump(mode="json") if plan.memory else None,
                "project_id": plan.project_id,
                "history": plan.history.model_dump(mode="json") if plan.history else None,
                "skill": plan.skill.model_dump(mode="json") if plan.skill else None,
            }
        ).model_dump(mode="json")
        connection.execute(
            authorizations.insert().values(
                grant_id=grant_id,
                task_id=task["task_id"],
                issued_by=owner,
                basis=basis,
                body=body,
                body_sha256=content_hash(body),
            )
        )
        return grant_id

    def _parent(self, connection, task, plan, *, delivery=False):
        seen = {task["task_id"]}
        while task["parent_id"]:
            if task["parent_id"] in seen or len(seen) > 8:
                raise AccessDenied("invalid_parent_lineage")
            seen.add(task["parent_id"])
            parent = self._task(connection, task["parent_id"], task["owner_id"])
            if (
                parent["revision_id"] != task["parent_revision_id"]
                or parent["grant_id"] != task["parent_grant_id"]
                or parent["epoch"] != task["parent_epoch"]
            ):
                raise AccessDenied("parent_revision_or_generation_changed")
            grant = checked_grant(connection, parent["grant_id"])
            if (
                parent["state"]
                not in ({"DRAFT", "RUNNING", "COMPLETED"} if delivery else {"DRAFT", "RUNNING"})
                or not active_grant(grant, datetime.now(UTC))
                or not within(plan, grant["body"])
            ):
                raise AccessDenied("subtask_exceeds_parent_authorization")
            task = parent

    def _waiting(self, connection, task, plan, grant, *, revoked=False):
        missing = missing_conditions(plan)
        frozen = checked_revision(connection, task["revision_id"])["frozen_time"]
        if (
            plan.request.expected_time
            and plan.request.expected_time.model_dump(mode="json") != frozen
        ):
            missing.append("accept_frozen_time")
        if missing:
            state, kind, fields = "WAITING_INPUT", "input", missing
        elif (
            revoked or not active_grant(grant, datetime.now(UTC)) or not within(plan, grant["body"])
        ):
            state, kind, fields = "WAITING_APPROVAL", "approval", []
        else:
            return "DRAFT"
        connection.execute(
            questions.insert().values(
                question_id=identifier("Q-"),
                task_id=task["task_id"],
                revision_id=task["revision_id"],
                epoch=task["epoch"],
                kind=kind,
                fields=fields,
            )
        )
        return state

    def submit(self, owner, submission: Submission):
        plan, frozen_time = self._plan(submission.plan)
        self.memory.check_project(owner, plan.memory)
        self._history_project(owner, plan)
        if plan.request.task_id != submission.task_id:
            raise ValueError("request_task_differs_from_submission")
        with self.catalog.transaction() as connection:
            if row(connection, tasks, "task_id", submission.task_id):
                raise ControlConflict("task_already_exists")
            parent = (
                self._task(connection, submission.parent_id, owner)
                if submission.parent_id
                else None
            )
            task = {
                "task_id": submission.task_id,
                "owner_id": owner,
                "parent_id": submission.parent_id,
                "parent_revision_id": parent["revision_id"] if parent else None,
                "parent_grant_id": parent["grant_id"] if parent else None,
                "parent_epoch": parent["epoch"] if parent else None,
                "state": "DRAFT",
                "version": 1,
                "epoch": 1,
                "revision_id": None,
                "grant_id": None,
                "execution_id": None,
                "published_report": None,
                "quality": {"semantic_review": "pending"},
            }
            connection.execute(tasks.insert().values(**task))
            self._parent(connection, task, plan)
            revision_id = self._revision(connection, task, plan, frozen_time)
            # Authenticated task submission is the trusted human action; document content is never parsed as approval.
            grant_id = (
                self._grant(
                    connection,
                    task,
                    plan,
                    frozen_time,
                    owner,
                    "user_submission" if not parent else "user_subtask_submission_within_parent",
                )
                if not missing_conditions(plan)
                and (
                    plan.request.expected_time is None
                    or plan.request.expected_time.model_dump(mode="json") == frozen_time
                )
                else None
            )
            task.update(revision_id=revision_id, grant_id=grant_id)
            connection.execute(
                tasks.update()
                .where(tasks.c.task_id == task["task_id"])
                .values(revision_id=revision_id, grant_id=grant_id)
            )
            target = self._waiting(connection, task, plan, checked_grant(connection, grant_id))
            task = self._event(connection, task, owner, "submitted", target=target)
        return self.view(owner, task["task_id"])

    def view(self, owner, task_id):
        with self.catalog.engine.begin() as connection:
            task = self._task(connection, task_id, owner)
            revision = checked_revision(connection, task["revision_id"])
            grant = checked_grant(connection, task["grant_id"])
            pending = [
                dict(q)
                for q in connection.execute(
                    sa.select(questions).where(
                        questions.c.task_id == task_id,
                        questions.c.revision_id == task["revision_id"],
                        questions.c.epoch == task["epoch"],
                        questions.c.answer.is_(None),
                    )
                ).mappings()
            ]
            public_task = {k: v for k, v in task.items() if k not in {"execution_id"}}
            public_task["published_report"] = (
                {"sha256": task["published_report"]["report_artifact"]["sha256"]}
                if task["published_report"] and active_grant(grant, datetime.now(UTC))
                else None
            )
            for question in pending:
                question["prompt"] = (
                    "请明确批准当前计划版本中的范围与执行能力变更。"
                    if question["kind"] == "approval"
                    else clarification_prompt(
                        question["fields"], intent=revision["body"]["request"]["intent"]
                    )
                )
            return {
                **public_task,
                "plan": revision["body"],
                "plan_sha256": revision["body_sha256"],
                "binding_task_id": revision["binding_task_id"],
                "frozen_time": revision["frozen_time"],
                "authorization": {
                    "grant_id": grant["grant_id"],
                    "active": active_grant(grant, datetime.now(UTC)),
                    "basis": grant["basis"],
                    "scope": grant["body"],
                }
                if grant
                else None,
                "questions": pending,
            }

    def revise(self, owner, task_id, expected, proposed: Plan):
        plan, frozen_time = self._plan(proposed)
        self.memory.check_project(owner, plan.memory)
        self._history_project(owner, plan)
        with self.catalog.transaction() as connection:
            task = self._task(connection, task_id, owner)
            self._version(task, expected)
            if task["state"] not in {
                "DRAFT",
                "WAITING_INPUT",
                "WAITING_APPROVAL",
                "PAUSED",
                "FAILED",
            }:
                raise ControlConflict("pause_before_revision_or_task_terminal")
            if plan.request.task_id != task_id:
                raise ValueError("request_task_differs_from_revision")
            self._parent(connection, task, plan)
            old = task["revision_id"]
            revision_id = self._revision(connection, task, plan, frozen_time, old)
            task = self._event(
                connection,
                task,
                owner,
                "plan_revised",
                target="DRAFT",
                changes={
                    "revision_id": revision_id,
                    "epoch": task["epoch"] + 1,
                    "execution_id": None,
                },
            )
            grant = checked_grant(connection, task["grant_id"])
            target = self._waiting(connection, task, plan, grant)
            if target != "DRAFT":
                self._event(connection, task, owner, "revision_waiting", target=target)
        return self.view(owner, task_id)

    def answer(self, owner, task_id, answer: InputAnswer):
        current = self.view(owner, task_id)
        self._version(current, answer.expected_version)
        pending = next(
            (
                q
                for q in current["questions"]
                if q["question_id"] == answer.question_id and q["kind"] == "input"
            ),
            None,
        )
        if (
            current["state"] != "WAITING_INPUT"
            or pending is None
            or not answer.values
            or not set(answer.values) <= set(pending["fields"])
        ):
            raise ControlConflict("stale_or_invalid_input_question")
        value = current["plan"]
        values = dict(answer.values)
        if "accept_frozen_time" in values:
            if values.pop("accept_frozen_time") is not True:
                raise ControlConflict("frozen_time_not_accepted")
            value["request"]["expected_time"] = None
        value["scope"] |= values
        plan, frozen_time = self._plan(Plan.model_validate(value))
        with self.catalog.transaction() as connection:
            task = self._task(connection, task_id, owner)
            self._version(task, answer.expected_version)
            self._parent(connection, task, plan)
            question = row(connection, questions, "question_id", answer.question_id)
            if (
                question is None
                or question["revision_id"] != task["revision_id"]
                or question["epoch"] != task["epoch"]
                or question["answer"] is not None
            ):
                raise ControlConflict("input_question_changed")
            revision_id = self._revision(connection, task, plan, frozen_time, task["revision_id"])
            grant_id = task["grant_id"]
            # Missing required fields never create a wildcard grant. The authenticated user's
            # answer can complete the original scoped submission; revocation needs explicit approval.
            if (
                grant_id is None
                and not missing_conditions(plan)
                and plan.request.expected_time is None
            ):
                grant_id = self._grant(
                    connection, task, plan, frozen_time, owner, "user_clarification"
                )
            task = self._event(
                connection,
                task,
                owner,
                "input_answered",
                target="DRAFT",
                changes={
                    "revision_id": revision_id,
                    "grant_id": grant_id,
                    "epoch": task["epoch"] + 1,
                },
            )
            target = self._waiting(connection, task, plan, checked_grant(connection, grant_id))
            if target != "DRAFT":
                self._event(connection, task, owner, "clarification_waiting", target=target)
            connection.execute(
                questions.update()
                .where(questions.c.question_id == answer.question_id, questions.c.answer.is_(None))
                .values(answer=answer.model_dump(mode="json"))
            )
        return self.view(owner, task_id)

    def approve(self, owner, task_id, approval: Approval):
        with self.catalog.transaction() as connection:
            task = self._task(connection, task_id, owner)
            self._version(task, approval.expected_version)
            revision = checked_revision(connection, task["revision_id"])
            question = row(connection, questions, "question_id", approval.question_id)
            if (
                task["state"] != "WAITING_APPROVAL"
                or task["revision_id"] != approval.revision_id
                or revision["body_sha256"] != approval.plan_sha256
                or question is None
                or question["task_id"] != task_id
                or question["revision_id"] != task["revision_id"]
                or question["epoch"] != task["epoch"]
                or question["kind"] != "approval"
                or question["answer"] is not None
            ):
                raise ControlConflict("stale_or_mismatched_approval")
            connection.execute(
                questions.update()
                .where(questions.c.question_id == question["question_id"])
                .values(answer=approval.model_dump(mode="json"))
            )
            if not approval.approve:
                self._event(
                    connection,
                    task,
                    owner,
                    "approval_declined",
                    target="PAUSED",
                    changes={"epoch": task["epoch"] + 1},
                )
            else:
                plan = Plan.model_validate(revision["body"])
                self._parent(connection, task, plan)
                if missing_conditions(plan):
                    raise ControlConflict("business_conditions_missing")
                old = checked_grant(connection, task["grant_id"])
                if old and old["revoked_at"] is None:
                    connection.execute(
                        authorizations.update()
                        .where(authorizations.c.grant_id == old["grant_id"])
                        .values(revoked_at=datetime.now(UTC))
                    )
                grant = self._grant(
                    connection,
                    task,
                    plan,
                    revision["frozen_time"],
                    owner,
                    "explicit_revision_approval",
                )
                self._event(
                    connection,
                    task,
                    owner,
                    "authorization_approved",
                    target="DRAFT",
                    changes={"grant_id": grant, "epoch": task["epoch"] + 1},
                )
        return self.view(owner, task_id)

    def change_state(self, owner, task_id, expected, action):
        with self.catalog.transaction() as connection:
            task = self._task(connection, task_id, owner)
            self._version(task, expected)
            if task["state"] in {"COMPLETED", "CANCELLED"} and action != "revoke":
                raise ControlConflict("task_terminal")
            revision = checked_revision(connection, task["revision_id"])
            plan = Plan.model_validate(revision["body"])
            grant = checked_grant(connection, task["grant_id"])
            if action == "revoke":
                if grant and grant["revoked_at"] is None:
                    connection.execute(
                        authorizations.update()
                        .where(authorizations.c.grant_id == grant["grant_id"])
                        .values(revoked_at=datetime.now(UTC))
                    )
                target = (
                    task["state"]
                    if task["state"] in {"COMPLETED", "CANCELLED", "FAILED"}
                    else "PAUSED"
                )
            elif action == "pause":
                target = "PAUSED"
            elif action == "cancel":
                target = "CANCELLED"
            elif action == "resume":
                if task["state"] not in {"PAUSED", "FAILED"}:
                    raise ControlConflict("resume_requires_paused_or_failed")
                self._parent(connection, task, plan)
                target = self._waiting(connection, task | {"epoch": task["epoch"] + 1}, plan, grant)
            else:
                raise ValueError("unknown_lifecycle_action")
            if task["execution_id"]:
                connection.execute(
                    executions.update()
                    .where(
                        executions.c.execution_id == task["execution_id"],
                        executions.c.state == "running",
                    )
                    .values(state="interrupted")
                )
            task = self._event(
                connection,
                task,
                owner,
                action,
                target=target,
                changes={"epoch": task["epoch"] + 1, "execution_id": None},
            )
            if action == "cancel":
                affected = {task_id}
                candidates = [
                    dict(t)
                    for t in connection.execute(
                        sa.select(tasks).where(tasks.c.owner_id == owner)
                    ).mappings()
                ]
                while True:
                    children = [
                        t
                        for t in candidates
                        if t["parent_id"] in affected and t["task_id"] not in affected
                    ]
                    if not children:
                        break
                    for child in children:
                        affected.add(child["task_id"])
                        if child["state"] not in {"COMPLETED", "CANCELLED"}:
                            if child["execution_id"]:
                                connection.execute(
                                    executions.update()
                                    .where(executions.c.execution_id == child["execution_id"])
                                    .values(state="interrupted")
                                )
                            self._event(
                                connection,
                                child,
                                owner,
                                "ancestor_cancelled",
                                target="CANCELLED",
                                changes={"epoch": child["epoch"] + 1, "execution_id": None},
                            )
        if action == "cancel":
            self._close_bindings(affected)
        if self.dispatcher is not None:
            self.dispatcher.recover_expired()
        return self.view(owner, task_id)

    def _close_bindings(self, task_ids):
        with self.catalog.engine.begin() as connection:
            values = [
                dict(r)
                for r in connection.execute(
                    sa.select(revisions).where(revisions.c.task_id.in_(task_ids))
                ).mappings()
            ]
        for revision in values:
            with self.catalog.engine.begin() as connection:
                task = row(connection, tasks, "task_id", revision["task_id"])
            if task["state"] in {"COMPLETED", "CANCELLED"} and self._binding_exists(
                revision["binding_task_id"]
            ):
                with using(Access(task["task_id"], revision["revision_id"], kind="close")):
                    self.catalog.finish(
                        revision["binding_task_id"],
                        state="cancelled" if task["state"] == "CANCELLED" else "finished",
                    )

    def _binding_exists(self, binding):
        from database.schema import release_tasks

        with self.catalog.engine.begin() as connection:
            return row(connection, release_tasks, "task_id", binding) is not None

    def _ensure_binding(self, task, revision):
        plan = Plan.model_validate(revision["body"])
        with using(Access(task["task_id"], revision["revision_id"], kind="binding")):
            self.catalog.bind(
                BindTask(
                    task_id=revision["binding_task_id"],
                    release_id=plan.release_id,
                    allowed_source_ids=plan.allowed_source_ids,
                    scope=plan.scope,
                )
            )

    def research(self, owner, task_id, request: Research):
        with self.catalog.transaction() as connection:
            task = self._task(connection, task_id, owner)
            from control.queue_schema import pools

            ancestor = task
            while ancestor["parent_id"]:
                ancestor = self._task(connection, ancestor["parent_id"], owner)
            if row(connection, pools, "root_task_id", ancestor["task_id"]):
                raise AccessDenied("budgeted_task_research_requires_dispatched_tools")
            access = Access(task_id, task["revision_id"], task["grant_id"], task["epoch"])
            _, revision, _ = validate(connection, access, "research", datetime.now(UTC))
        self._ensure_binding(task, revision)
        query = ReleaseQuery(
            task_id=revision["binding_task_id"],
            query=request.query,
            expected_release_id=revision["body"]["release_id"],
            method="bm25",
            top_k=6,
        )
        with using(access):
            if request.operation == "search":
                result = self.service.query(query)
            elif request.operation == "count":
                result = self.service.count(query, request.components)
            else:
                plan = Plan.model_validate(revision["body"])
                if len(plan.request.pairs) != 1:
                    raise ValueError("research_links_require_one_pinned_pair")
                pair = plan.request.pairs[0]
                result = self.service.links(query, pair.left, pair.right)
            # Read output is not released if authorization changed while the tool was running.
            with self.catalog.engine.begin() as connection:
                validate(connection, access, "research", datetime.now(UTC))
        return {"status": "researched", "formal_deliverable": False, "result": result}

    def execute(self, owner, task_id, expected):
        if self.dispatcher is None:
            with self.catalog.engine.begin() as connection:
                candidate = self._task(connection, task_id, owner)
                selected = checked_revision(connection, candidate["revision_id"])["body"].get(
                    "investigator"
                )
            if selected in {"multi_agent", "parallel_tools"}:
                from control.dispatch import Dispatcher

                Dispatcher(self)
        if self.dispatcher is not None:
            return self.dispatcher.execute_sync(owner, task_id, expected)
        from control.queue_schema import pools

        with self.catalog.transaction() as connection:
            task = self._task(connection, task_id, owner)
            ancestor = task
            while ancestor["parent_id"]:
                ancestor = self._task(connection, ancestor["parent_id"], owner)
            if row(connection, pools, "root_task_id", ancestor["task_id"]):
                raise AccessDenied("durable_dispatch_required_for_budgeted_task")
            task, revision, plan, access = self._start_execution(
                connection, owner, task_id, expected
            )
        return self._run_execution(owner, task, revision, plan, access)

    def _start_execution(
        self, connection, owner, task_id, expected, *, advance_epoch=False, worker_id=None
    ):
        secret = secrets.token_urlsafe(32)
        task = self._task(connection, task_id, owner)
        self._version(task, expected)
        revision = checked_revision(connection, task["revision_id"])
        plan = Plan.model_validate(revision["body"])
        if task["state"] != "DRAFT" or plan.mode != "EXECUTE":
            raise AccessDenied("execute_requires_execution_plan_and_ready_state")
        validate(
            connection,
            Access(task_id, task["revision_id"], task["grant_id"], task["epoch"]),
            "research",
            datetime.now(UTC),
        )
        grant = checked_grant(connection, task["grant_id"])
        if "generate" not in grant["body"]["operations"] or missing_conditions(plan):
            raise AccessDenied("execution_not_authorized_or_conditions_missing")
        execution_id = identifier("EXEC-")
        epoch = task["epoch"] + int(advance_epoch)
        connection.execute(
            executions.insert().values(
                execution_id=execution_id,
                task_id=task_id,
                revision_id=task["revision_id"],
                grant_id=task["grant_id"],
                epoch=epoch,
                permit_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                state="running",
            )
        )
        task = self._event(
            connection,
            task,
            owner,
            "execution_started",
            target="RUNNING",
            changes={"execution_id": execution_id, "epoch": epoch},
            payload={"worker_id": worker_id} if worker_id else None,
        )
        access = Access(
            task_id, task["revision_id"], task["grant_id"], epoch, "run", execution_id, secret
        )
        return task, revision, plan, access

    def _run_execution(self, owner, task, revision, plan, access, *, model=None, config=None):
        task_id, execution_id = task["task_id"], access.execution_id
        model = self.model if model is None else model
        config = self.config if config is None else config
        from memory.episode_runtime import HistoryBinding, using_history
        from memory.procedure_runtime import SkillBinding, using_skill
        from memory.runtime import Binding
        from memory.runtime import using as using_memory

        memory_binding = None
        history_binding = None
        skill_binding = None
        try:
            self._ensure_binding(task, revision)
            request = InvestigationRequest.model_validate(
                plan.request.model_dump(mode="json")
                | {"task_id": revision["binding_task_id"], "expected_release_id": plan.release_id}
            )
            if plan.memory and plan.memory.enabled:
                memory_binding = Binding(self.memory, owner, plan, access)
            if plan.history and plan.history.enabled:
                history_binding = HistoryBinding(self.episodes, owner, plan, access)
            if plan.skill and plan.skill.enabled:
                skill_binding = SkillBinding(self.procedures, owner, plan, access)
            with (
                using(access),
                using_memory(memory_binding),
                using_history(history_binding),
                using_skill(skill_binding),
            ):
                from typing import Any

                runner: Any
                if plan.investigator == "multi_agent":
                    from investigation.cooperation import CooperativeInvestigator

                    runner = CooperativeInvestigator(
                        self.service,
                        model,
                        self.root / "private-runs",
                        config,
                        plan_snapshot={
                            "goal": plan.request.question,
                            "hypotheses": plan.hypotheses,
                            "steps": plan.steps,
                        },
                    )
                elif plan.investigator in {"single_agent", "parallel_tools"}:
                    from investigation.agent import SingleAgent

                    runner = SingleAgent(
                        self.service,
                        model,
                        self.root / "private-runs",
                        config,
                        plan_snapshot={
                            "goal": plan.request.question,
                            "hypotheses": plan.hypotheses,
                            "steps": plan.steps,
                            "control_revision_id": revision["revision_id"],
                        },
                        parallel_tools=plan.investigator == "parallel_tools",
                    )
                else:
                    runner = FixedInvestigator(
                        self.service, model, self.root / "private-runs", config
                    )
                result = runner.run(request)
            # Only this transaction publishes. Revocation and publication serialize on the catalog lock.
            with self.catalog.transaction() as connection:
                task, _, _ = validate(connection, access, "publish", datetime.now(UTC))
                if memory_binding:
                    memory_binding.validate(connection)
                if history_binding:
                    history_binding.validate(connection)
                    history_binding.validate_report(result)
                if skill_binding:
                    skill_binding.validate(connection)
                    skill_binding.validate_report(result)
                if result["execution_status"] != "completed":
                    connection.execute(
                        executions.update()
                        .where(executions.c.execution_id == execution_id)
                        .values(
                            state="failed",
                            private_report={
                                k: result[k] for k in ("report_artifact", "artifact_root")
                            },
                        )
                    )
                    self._event(
                        connection,
                        task,
                        owner,
                        "execution_failed",
                        target="FAILED",
                        changes={"execution_id": None, "epoch": task["epoch"] + 1},
                    )
                elif result["proposal"]["disposition"] == "needs_clarification":
                    self._event(
                        connection,
                        task,
                        owner,
                        "execution_needs_input",
                        target="WAITING_INPUT",
                        changes={"execution_id": None, "epoch": task["epoch"] + 1},
                    )
                else:
                    connection.execute(
                        executions.update()
                        .where(executions.c.execution_id == execution_id)
                        .values(
                            state="completed",
                            private_report={
                                k: result[k] for k in ("report_artifact", "artifact_root")
                            },
                        )
                    )
                    self._event(
                        connection,
                        task,
                        owner,
                        "report_published",
                        target="COMPLETED",
                        changes={
                            "execution_id": None,
                            "published_report": {
                                k: result[k] for k in ("report_artifact", "artifact_root")
                            },
                            "quality": {
                                "disposition": result["proposal"]["disposition"],
                                "semantic_review": "pending",
                            },
                        },
                    )
                if (
                    result["proposal"] is None
                    or result["proposal"]["disposition"] != "needs_clarification"
                ):
                    self._capture_episode(connection, owner, task_id, plan)
            final = self.view(owner, task_id)
            if final["state"] == "COMPLETED":
                self._close_bindings([task_id])
            return final
        except Exception as error:
            with self.catalog.transaction() as connection:
                current = self._task(connection, task_id, owner)
                # A late complete run can be retained privately, never published by an old lease.
                if "result" in locals():
                    connection.execute(
                        executions.update()
                        .where(
                            executions.c.execution_id == execution_id,
                            executions.c.private_report.is_(None),
                        )
                        .values(
                            private_report={
                                k: result[k] for k in ("report_artifact", "artifact_root")
                            }
                        )
                    )
                if current["execution_id"] == execution_id and current["state"] == "RUNNING":
                    connection.execute(
                        executions.update()
                        .where(executions.c.execution_id == execution_id)
                        .values(state="failed")
                    )
                    self._event(
                        connection,
                        current,
                        owner,
                        "execution_exception",
                        target="PAUSED" if isinstance(error, AccessDenied) else "FAILED",
                        changes={"execution_id": None, "epoch": current["epoch"] + 1},
                        payload={"error_type": type(error).__name__},
                    )
                    self._capture_episode(connection, owner, task_id, plan)
            if isinstance(error, AccessDenied):
                return self.view(owner, task_id)
            raise

    def report(self, owner, task_id):
        with self.catalog.transaction() as connection:
            report = self._published_report(connection, owner, task_id)[1]
            if "memory_usage" in report:
                report = report | {
                    "memory_audit": self.memory.audit(
                        connection, owner, report["memory_usage"]["references"]
                    )
                }
            if "history_usage" in report:
                report = report | {
                    "history_audit": self.episodes.audit(
                        connection, owner, report["history_usage"]["references"]
                    )
                }
            if "skill_usage" in report:
                report = report | {
                    "skill_audit": self.procedures.audit(
                        connection, owner, report["skill_usage"]["reference"]
                    )
                }
            return report

    def _published_report(self, connection, owner, task_id):
        task = self._task(connection, task_id, owner)
        if task["state"] != "COMPLETED" or not task["published_report"]:
            raise AccessDenied("no_published_report")
        grant = checked_grant(connection, task["grant_id"])
        if not active_grant(grant, datetime.now(UTC)):
            raise AccessDenied("report_authorization_revoked")
        plan = Plan.model_validate(checked_revision(connection, task["revision_id"])["body"])
        if not within(plan, grant["body"]):
            raise AccessDenied("report_exceeds_authorization")
        self._parent(connection, task, plan, delivery=True)
        ref = task["published_report"]
        return ref, ArtifactStore(Path(ref["artifact_root"])).read_json(
            ref["report_artifact"]["sha256"]
        )

    def history(self, owner, task_id):
        with self.catalog.engine.begin() as connection:
            self._task(connection, task_id, owner)
            result = {}
            for name, table in (
                ("revisions", revisions),
                ("authorizations", authorizations),
                ("events", events),
                ("questions", questions),
                ("executions", executions),
            ):
                result[name] = [
                    {k: v for k, v in row_.items() if k not in {"permit_sha256", "private_report"}}
                    for row_ in connection.execute(
                        sa.select(table).where(table.c.task_id == task_id)
                    ).mappings()
                ]
            result["events"].sort(key=lambda e: e["version"])
            return result

    def inspect(
        self, owner, task_id, *, section, alias=None, artifact_id=None, offset=0, limit=2000
    ):
        from investigation.inspection import inspect_report

        # Hold the catalog transaction through disclosure so revocation/publication serialize.
        with self.catalog.transaction() as connection:
            ref, report = self._published_report(connection, owner, task_id)
            _, manifest = self.catalog._available(connection, report["release_id"])
            result = inspect_report(
                report,
                ArtifactStore(Path(ref["artifact_root"])),
                self.catalog.store.directory(manifest),
                section=section,
                alias=alias,
                artifact_id=artifact_id,
                offset=offset,
                limit=limit,
            )
            return result

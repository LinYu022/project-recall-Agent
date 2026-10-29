"""PostgreSQL dispatch: claims are capabilities, queue rows are not execution permission."""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa

from control.access import (
    Access,
    AccessDenied,
    ControlConflict,
    checked_grant,
    checked_revision,
    row,
    validate,
)
from control.contracts import Plan
from control.queue_contracts import Enqueue, add, fits, vector
from control.queue_schema import calls, jobs, pools
from control.schema import executions
from control.service import identifier
from ingestion.records import content_hash
from investigation.contracts import FixedConfig
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def db_now(connection):
    return connection.scalar(sa.select(sa.func.clock_timestamp()))


def implementation_digest():
    paths = sorted(
        [
            *Path("control").glob("*.py"),
            *Path("investigation").glob("*.py"),
            *Path("memory").glob("*.py"),
            Path("runtime/durable.py"),
            Path("models/chat_api.py"),
        ]
    )
    return content_hash({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


@dataclass
class Claim:
    job_id: str
    access: Access
    task: dict
    revision: dict
    plan: Plan
    config: FixedConfig
    lease_seconds: int


class Dispatcher:
    def __init__(self, control):
        self.control, self.catalog = control, control.catalog
        control.dispatcher = self

    def _config(self):
        return self.control.config or FixedConfig(max_model_calls=6, context_mode="structured")

    def store(self, job_id):
        return ArtifactStore(self.control.root / "q" / job_id)

    def _pool(self, connection, root):
        pool = row(connection, pools, "root_task_id", root)
        if not pool or content_hash(pool["limits"]) != pool["limits_sha256"]:
            raise ArtifactIntegrityError("dispatch pool limits changed")
        return pool

    def enqueue(self, owner, task_id, value: Enqueue):
        value = Enqueue.model_validate(value.model_dump(mode="json"))
        with self.catalog.transaction() as connection:
            task = self.control._task(connection, task_id, owner)
            existing = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.owner_id == owner,
                        jobs.c.task_id == task_id,
                        jobs.c.idempotency_key == value.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["pinned"].get("submission_sha256") != content_hash(
                    value.model_dump(mode="json")
                ):
                    raise ControlConflict("idempotency_key_reused_with_different_request")
                return self._public(dict(existing))
            self.control._version(task, value.expected_version)
            revision = checked_revision(connection, task["revision_id"])
            plan = Plan.model_validate(revision["body"])
            now = db_now(connection)
            validate(
                connection,
                Access(task_id, task["revision_id"], task["grant_id"], task["epoch"]),
                "research",
                now,
            )
            grant = checked_grant(connection, task["grant_id"])
            if (
                plan.mode != "EXECUTE"
                or task["state"] != "DRAFT"
                or "generate" not in grant["body"]["operations"]
            ):
                raise AccessDenied("ready_execution_plan_required")
            if connection.scalar(
                sa.select(jobs.c.job_id).where(
                    jobs.c.task_id == task_id,
                    jobs.c.state.in_(["queued", "running", "waiting_input"]),
                )
            ):
                raise ControlConflict("task_already_dispatched")
            root = task
            while root["parent_id"]:
                root = self.control._task(connection, root["parent_id"], owner)
            config = self._config()
            if row(connection, pools, "root_task_id", root["task_id"]) is None:
                limits = vector(
                    model_calls=config.max_model_calls,
                    tool_calls=config.max_tool_calls,
                    prompt_characters=config.total_prompt_characters,
                    completion_tokens=config.total_generation_tokens,
                )
                connection.execute(
                    pools.insert().values(
                        root_task_id=root["task_id"],
                        owner_id=owner,
                        limits=limits,
                        limits_sha256=content_hash(limits),
                        used=vector(),
                        held=vector(),
                    )
                )
            _, manifest = self.catalog._available(connection, plan.release_id)
            pinned = {
                "submission_sha256": content_hash(value.model_dump(mode="json")),
                "revision_id": task["revision_id"],
                "grant_id": task["grant_id"],
                "plan_sha256": revision["body_sha256"],
                "grant_sha256": grant["body_sha256"],
                "release_id": plan.release_id,
                "manifest_sha256": content_hash(manifest),
                "model_identity": self.control.model.identity,
                "config": config.model_dump(mode="json"),
                "implementation_sha256": implementation_digest(),
            }
            deadline = value.deadline or now + timedelta(seconds=config.wall_seconds)
            if deadline <= now or deadline > now + timedelta(seconds=1800):
                raise ValueError("deadline_must_be_future_and_within_1800_seconds")
            job = {
                "job_id": identifier("JOB-"),
                "owner_id": owner,
                "task_id": task_id,
                "root_task_id": root["task_id"],
                "idempotency_key": value.idempotency_key,
                "pinned": pinned,
                "pinned_sha256": content_hash(pinned),
                "state": "queued",
                "attempt": 0,
                "execution_id": None,
                "available_at": now,
                "deadline": deadline,
                "max_attempts": value.max_attempts,
                "lease_seconds": value.lease_seconds,
                "last_error": None,
            }
            connection.execute(jobs.insert().values(**job))
            self.control._event(
                connection,
                task,
                owner,
                "job_enqueued",
                payload={"job_id": job["job_id"], "root_budget": root["task_id"]},
            )
        return self._public(job)

    def _public(self, job):
        return {
            k: job[k]
            for k in (
                "job_id",
                "task_id",
                "state",
                "attempt",
                "deadline",
                "available_at",
                "max_attempts",
                "lease_seconds",
                "last_error",
            )
        }

    def view(self, owner, task_id):
        with self.catalog.engine.begin() as connection:
            self.control._task(connection, task_id, owner)
            rows = (
                connection.execute(
                    sa.select(jobs).where(jobs.c.task_id == task_id).order_by(jobs.c.available_at)
                )
                .mappings()
                .all()
            )
            result = {"jobs": [self._public(dict(j)) for j in rows], "resources": None, "calls": []}
            if rows:
                result["resources"] = self._pool(connection, rows[-1]["root_task_id"])
                result["calls"] = [
                    {
                        k: v
                        for k, v in r.items()
                        if k not in {"input_artifact", "output_artifact", "fingerprint"}
                    }
                    for r in connection.execute(
                        sa.select(calls)
                        .where(calls.c.job_id.in_([j["job_id"] for j in rows]))
                        .order_by(calls.c.ordinal, calls.c.attempt)
                    ).mappings()
                ]
            return result

    def _check_pins(self, connection, job, task):
        p = job["pinned"]
        if content_hash(p) != job["pinned_sha256"]:
            raise ArtifactIntegrityError("job snapshot changed")
        revision = checked_revision(connection, task["revision_id"])
        grant = checked_grant(connection, task["grant_id"])
        if task["revision_id"] != p["revision_id"] or task["grant_id"] != p["grant_id"]:
            raise AccessDenied("queued_revision_or_authorization_changed")
        if (
            revision["body_sha256"] != p["plan_sha256"]
            or not grant
            or grant["body_sha256"] != p["grant_sha256"]
        ):
            raise ArtifactIntegrityError("pinned_plan_or_grant_changed")
        if (
            self.control.model.identity != p["model_identity"]
            or implementation_digest() != p["implementation_sha256"]
            or self._config().model_dump(mode="json") != p["config"]
        ):
            raise ArtifactIntegrityError("recorded_runtime_unavailable")
        _, manifest = self.catalog._available(connection, p["release_id"])
        if content_hash(manifest) != p["manifest_sha256"]:
            raise ArtifactIntegrityError("pinned_release_changed")
        for call in connection.execute(
            sa.select(calls).where(calls.c.job_id == job["job_id"])
        ).mappings():
            self.store(job["job_id"]).read_json(call["input_artifact"]["sha256"])
            if call["output_artifact"]:
                self.store(job["job_id"]).read_json(call["output_artifact"]["sha256"])
        return revision

    def _job_end(self, connection, job, task, state, code, *, task_state=None):
        connection.execute(
            jobs.update().where(jobs.c.job_id == job["job_id"]).values(state=state, last_error=code)
        )
        if (
            task["state"] not in {"COMPLETED", "CANCELLED", "FAILED"}
            and task["revision_id"] == job["pinned"]["revision_id"]
            and task["grant_id"] == job["pinned"]["grant_id"]
            and task["execution_id"] in {None, job["execution_id"]}
            and task_state
        ):
            if task["execution_id"]:
                connection.execute(
                    executions.update()
                    .where(executions.c.execution_id == task["execution_id"])
                    .values(state="interrupted")
                )
            self.control._event(
                connection,
                task,
                job["owner_id"],
                "dispatch_" + state,
                target=task_state,
                changes={"execution_id": None, "epoch": task["epoch"] + 1},
                payload={"reason": code, "job_id": job["job_id"]},
            )

    def claim(self, worker_id, *, job_id=None):
        if not worker_id or len(worker_id) > 100:
            raise ValueError("invalid_worker_id")
        self.recover_expired()
        with self.catalog.transaction() as connection:
            now = db_now(connection)
            query = (
                sa.select(jobs)
                .where(jobs.c.state == "queued", jobs.c.available_at <= now)
                .order_by(jobs.c.available_at, jobs.c.job_id)
                .with_for_update(skip_locked=True)
            )
            if job_id:
                query = query.where(jobs.c.job_id == job_id)
            value = connection.execute(query.limit(1)).mappings().first()
            if value is None:
                return None
            job = dict(value)
            task = self.control._task(connection, job["task_id"], job["owner_id"])
            if task["state"] in {"CANCELLED", "COMPLETED", "FAILED"}:
                self._job_end(
                    connection,
                    job,
                    task,
                    "cancelled" if task["state"] == "CANCELLED" else "failed",
                    "task_not_executable",
                )
                return None
            if task["state"] != "DRAFT":
                self._job_end(connection, job, task, "waiting_input", "task_paused_or_waiting")
                return None
            if now >= job["deadline"] or job["attempt"] >= job["max_attempts"]:
                self._job_end(
                    connection,
                    job,
                    task,
                    "failed",
                    "deadline_or_attempts_exhausted",
                    task_state="FAILED",
                )
                return None
            try:
                self._check_pins(connection, job, task)
                task, revision, plan, access = self.control._start_execution(
                    connection,
                    job["owner_id"],
                    task["task_id"],
                    task["version"],
                    advance_epoch=True,
                    worker_id=worker_id,
                )
            except (AccessDenied, ArtifactIntegrityError, ValueError, OSError) as error:
                self._job_end(
                    connection,
                    job,
                    task,
                    "failed",
                    type(error).__name__,
                    task_state="FAILED" if task["grant_id"] == job["pinned"]["grant_id"] else None,
                )
                return None
            connection.execute(
                jobs.update()
                .where(jobs.c.job_id == job["job_id"])
                .values(
                    state="running",
                    attempt=job["attempt"] + 1,
                    execution_id=access.execution_id,
                    last_error=None,
                )
            )
            connection.execute(
                executions.update()
                .where(executions.c.execution_id == access.execution_id)
                .values(
                    job_id=job["job_id"],
                    lease_until=min(
                        job["deadline"],
                        db_now(connection) + timedelta(seconds=job["lease_seconds"]),
                    ),
                    claim_version=task["version"],
                )
            )
            return Claim(
                job["job_id"],
                access,
                task,
                revision,
                plan,
                FixedConfig.model_validate(job["pinned"]["config"]),
                job["lease_seconds"],
            )

    def heartbeat(self, claim):
        with self.catalog.transaction() as connection:
            now = db_now(connection)
            validate(connection, claim.access, "research", now)
            job = row(connection, jobs, "job_id", claim.job_id)
            connection.execute(
                executions.update()
                .where(executions.c.execution_id == claim.access.execution_id)
                .values(
                    lease_until=min(job["deadline"], now + timedelta(seconds=claim.lease_seconds))
                )
            )

    def _abandon_reserved(self, connection, job):
        for call in connection.execute(
            sa.select(calls).where(calls.c.job_id == job["job_id"], calls.c.state == "reserved")
        ).mappings():
            pool = self._pool(connection, job["root_task_id"])
            connection.execute(
                pools.update()
                .where(pools.c.root_task_id == pool["root_task_id"])
                .values(
                    used=add(pool["used"], call["reserved"]),
                    held=add(pool["held"], call["reserved"], -1),
                )
            )
            connection.execute(
                calls.update()
                .where(calls.c.call_id == call["call_id"])
                .values(
                    state="unknown",
                    charged=call["reserved"],
                    pending_reconciliation=call["kind"] == "model",
                    error_code="worker_lost_after_reservation",
                )
            )

    def recover_expired(self):
        changed = []
        with self.catalog.transaction() as connection:
            now = db_now(connection)
            for value in (
                connection.execute(
                    sa.select(jobs).where(jobs.c.state.in_(["running", "queued", "waiting_input"]))
                )
                .mappings()
                .all()
            ):
                job = dict(value)
                task = self.control._task(connection, job["task_id"], job["owner_id"])
                if (
                    job["state"] == "waiting_input"
                    and now < job["deadline"]
                    and task["state"] != "CANCELLED"
                ):
                    continue
                execution = (
                    row(connection, executions, "execution_id", job["execution_id"])
                    if job["execution_id"]
                    else None
                )
                invalid = task["state"] in {
                    "CANCELLED",
                    "PAUSED",
                    "WAITING_INPUT",
                    "WAITING_APPROVAL",
                    "COMPLETED",
                    "FAILED",
                } or (execution and task["execution_id"] != execution["execution_id"])
                expired = execution and execution["lease_until"] <= now
                if not (invalid or expired or now >= job["deadline"]):
                    continue
                self._abandon_reserved(connection, job)
                if task["state"] == "COMPLETED":
                    published_here = execution and execution["state"] == "completed"
                    self._job_end(
                        connection,
                        job,
                        task,
                        "completed" if published_here else "failed",
                        "publication_recovered"
                        if published_here
                        else "superseded_by_another_execution",
                    )
                    changed.append(job["job_id"])
                    continue
                unknown = connection.scalar(
                    sa.select(calls.c.call_id).where(
                        calls.c.job_id == job["job_id"],
                        calls.c.kind == "model",
                        calls.c.state == "unknown",
                    )
                )
                if task["state"] == "CANCELLED":
                    self._job_end(
                        connection, job, task, "cancelled", "cancelled_upstream_may_still_run"
                    )
                elif now >= job["deadline"] or job["attempt"] >= job["max_attempts"]:
                    self._job_end(
                        connection,
                        job,
                        task,
                        "failed",
                        "deadline_or_attempts_exhausted",
                        task_state="FAILED",
                    )
                elif invalid:
                    self._job_end(
                        connection,
                        job,
                        task,
                        "waiting_input"
                        if task["state"] in {"PAUSED", "WAITING_INPUT", "WAITING_APPROVAL"}
                        else "failed",
                        "authorization_or_state_changed",
                    )
                elif unknown:
                    self._job_end(
                        connection,
                        job,
                        task,
                        "waiting_input",
                        "unknown_model_response_requires_reconciliation",
                        task_state="PAUSED",
                    )
                else:
                    connection.execute(
                        executions.update()
                        .where(executions.c.execution_id == job["execution_id"])
                        .values(state="interrupted")
                    )
                    self.control._event(
                        connection,
                        task,
                        job["owner_id"],
                        "lease_recovery",
                        target="PAUSED",
                        changes={"execution_id": None, "epoch": task["epoch"] + 1},
                    )
                    paused = self.control._task(connection, task["task_id"], job["owner_id"])
                    self.control._event(
                        connection, paused, job["owner_id"], "retry_scheduled", target="DRAFT"
                    )
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.job_id == job["job_id"])
                        .values(
                            state="queued",
                            execution_id=None,
                            available_at=now + timedelta(seconds=min(2 ** job["attempt"], 30)),
                            last_error="expired_worker_recovered",
                        )
                    )
                changed.append(job["job_id"])
        return changed

    def recover(self, owner, task_id, expected):
        self.recover_expired()
        with self.catalog.transaction() as connection:
            task = self.control._task(connection, task_id, owner)
            self.control._version(task, expected)
            job = (
                connection.execute(
                    sa.select(jobs)
                    .where(jobs.c.task_id == task_id, jobs.c.state == "waiting_input")
                    .order_by(jobs.c.available_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if job is None:
                raise ControlConflict("no_waiting_dispatch")
            job = dict(job)
            if task["state"] != "DRAFT":
                raise AccessDenied("resume_current_task_authorization_before_recovery")
            try:
                self._check_pins(connection, job, task)
            except (AccessDenied, ArtifactIntegrityError, OSError, ValueError):
                self._job_end(connection, job, task, "failed", "recovery_pins_unavailable")
            else:
                validate(
                    connection,
                    Access(task_id, task["revision_id"], task["grant_id"], task["epoch"]),
                    "research",
                    db_now(connection),
                )
                unknown = (
                    connection.execute(
                        sa.select(calls).where(
                            calls.c.job_id == job["job_id"],
                            calls.c.kind == "model",
                            calls.c.state == "unknown",
                        )
                    )
                    .mappings()
                    .first()
                )
                if unknown and unknown["pending_reconciliation"]:
                    raise ControlConflict("unknown_model_output_cannot_be_retried_automatically")
                if unknown:
                    self._job_end(
                        connection,
                        job,
                        task,
                        "failed",
                        "missing_model_response_after_reconciliation",
                        task_state="FAILED",
                    )
                else:
                    connection.execute(
                        jobs.update()
                        .where(jobs.c.job_id == job["job_id"])
                        .values(
                            state="queued",
                            execution_id=None,
                            available_at=db_now(connection),
                            last_error=None,
                        )
                    )
        return self.view(owner, task_id)

    def reserve(
        self, claim, ordinal, kind, fingerprint, requested, input_ref, *, lane=0, allocation=None
    ):
        requested = vector(**requested)
        with self.catalog.transaction() as connection:
            validate(
                connection,
                claim.access,
                "generate" if kind == "model" else "research",
                db_now(connection),
            )
            job = row(connection, jobs, "job_id", claim.job_id)
            pool = self._pool(connection, job["root_task_id"])
            if not lane * 100 <= ordinal < (lane + 1) * 100:
                raise ValueError("journal_lane_exhausted")
            if allocation is not None:
                spent = vector()
                for previous in connection.execute(
                    sa.select(calls).where(
                        calls.c.job_id == claim.job_id,
                        calls.c.ordinal >= lane * 100,
                        calls.c.ordinal < (lane + 1) * 100,
                    )
                ).mappings():
                    spent = add(spent, previous["charged"] or previous["reserved"])
                if not fits(vector(**allocation), spent, vector(), requested):
                    from models.chat_api import ModelCallFailed

                    raise ModelCallFailed("delegated_resource_budget_exhausted")
            if not fits(pool["limits"], pool["used"], pool["held"], requested):
                from models.chat_api import ModelCallFailed

                raise ModelCallFailed("shared_resource_budget_exhausted")
            attempt = 1 + (
                connection.scalar(
                    sa.select(sa.func.max(calls.c.attempt)).where(
                        calls.c.job_id == claim.job_id, calls.c.ordinal == ordinal
                    )
                )
                or 0
            )
            call_id = identifier("CALL-")
            connection.execute(
                calls.insert().values(
                    call_id=call_id,
                    job_id=claim.job_id,
                    execution_id=claim.access.execution_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    kind=kind,
                    fingerprint=fingerprint,
                    state="reserved",
                    reserved=requested,
                    charged=None,
                    input_artifact=input_ref,
                    output_artifact=None,
                    usage=None,
                    pending_reconciliation=False,
                    late=False,
                    error_code=None,
                )
            )
            connection.execute(
                pools.update()
                .where(pools.c.root_task_id == pool["root_task_id"])
                .values(held=add(pool["held"], requested))
            )
            return call_id

    def settle(
        self, claim, call_id, *, output_ref=None, usage=None, error_code=None, unknown=False
    ):
        with self.catalog.transaction() as connection:
            call = row(connection, calls, "call_id", call_id)
            if (
                call is None
                or call["execution_id"] != claim.access.execution_id
                or call["job_id"] != claim.job_id
            ):
                raise AccessDenied("call_does_not_belong_to_execution")
            job = row(connection, jobs, "job_id", call["job_id"])
            pool = self._pool(connection, job["root_task_id"])
            charged = dict(call["reserved"])
            completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
            known = type(completion) is int and 0 <= completion <= charged["completion_tokens"]
            if call["kind"] == "model" and known:
                charged["completion_tokens"] = completion
            try:
                validate(connection, claim.access, "research", db_now(connection))
                late = False
            except AccessDenied:
                late = True
            if call["state"] == "reserved":
                newused = add(pool["used"], charged)
                newheld = add(pool["held"], call["reserved"], -1)
            elif call["state"] == "unknown":
                newused = add(add(pool["used"], call["charged"], -1), charged)
                newheld = pool["held"]
            else:
                raise ControlConflict("call_already_settled")
            connection.execute(
                pools.update()
                .where(pools.c.root_task_id == pool["root_task_id"])
                .values(used=newused, held=newheld)
            )
            connection.execute(
                calls.update()
                .where(calls.c.call_id == call_id)
                .values(
                    state="unknown" if unknown else "error" if error_code else "completed",
                    charged=charged,
                    output_artifact=output_ref,
                    usage={"provider_usage": usage, "previous_accounting": call["usage"]}
                    if isinstance(call.get("usage"), dict) and call["usage"].get("accounting_basis")
                    else usage,
                    pending_reconciliation=call["kind"] == "model" and not known,
                    late=late,
                    error_code=error_code,
                )
            )

    def reconcile(self, owner, task_id, value):
        with self.catalog.transaction() as connection:
            self.control._task(connection, task_id, owner)
            call = row(connection, calls, "call_id", value.call_id)
            job = row(connection, jobs, "job_id", call["job_id"]) if call else None
            if not job or job["task_id"] != task_id or job["owner_id"] != owner:
                raise AccessDenied("call_not_owned")
            if (
                not call["pending_reconciliation"]
                or call["charged"] is None
                or value.completion_tokens > call["reserved"]["completion_tokens"]
            ):
                raise ControlConflict("invalid_or_repeated_reconciliation")
            pool = self._pool(connection, job["root_task_id"])
            charged = call["charged"] | {"completion_tokens": value.completion_tokens}
            connection.execute(
                pools.update()
                .where(pools.c.root_task_id == pool["root_task_id"])
                .values(used=add(add(pool["used"], call["charged"], -1), charged))
            )
            connection.execute(
                calls.update()
                .where(calls.c.call_id == call["call_id"])
                .values(
                    charged=charged,
                    pending_reconciliation=False,
                    usage={
                        "completion_tokens": value.completion_tokens,
                        "previous_provider_usage": call["usage"],
                        "accounting_basis": "user_attested_not_provider_verified",
                        "note": value.note,
                    },
                )
            )
        return self.view(owner, task_id)

    def execute_sync(self, owner, task_id, expected):
        from control.worker import Worker

        job = self.enqueue(
            owner, task_id, Enqueue(expected_version=expected, idempotency_key=f"sync-{expected}")
        )
        Worker(self, "inline").run_once(job_id=job["job_id"])
        return self.control.view(owner, task_id)

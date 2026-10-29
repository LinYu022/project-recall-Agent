"""In-process capabilities are issued by the trusted service, never accepted as request JSON."""

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa

from control.contracts import AuthorizationBoundary, Plan, active_grant, within
from control.schema import authorizations, executions, revisions, tasks
from ingestion.records import content_hash


class AccessDenied(PermissionError):
    pass


class ControlConflict(ValueError):
    pass


@dataclass(frozen=True)
class Access:
    task_id: str
    revision_id: str
    grant_id: str | None = None
    epoch: int = 0
    kind: str = "research"
    execution_id: str | None = None
    secret: str | None = None


CURRENT: ContextVar[Access | None] = ContextVar("control_access", default=None)


@contextmanager
def using(access: Access):
    token = CURRENT.set(access)
    try:
        yield
    finally:
        CURRENT.reset(token)


def row(connection, table, key, value):
    found = (
        connection.execute(sa.select(table).where(table.c[key] == value)).mappings().one_or_none()
    )
    return dict(found) if found else None


def checked_revision(connection, identifier):
    value = row(connection, revisions, "revision_id", identifier)
    if value is None or content_hash(value["body"]) != value["body_sha256"]:
        raise AccessDenied("revision_integrity_failed")
    return value


def checked_grant(connection, identifier):
    value = row(connection, authorizations, "grant_id", identifier) if identifier else None
    if value and content_hash(value["body"]) != value["body_sha256"]:
        raise AccessDenied("authorization_integrity_failed")
    if value:
        AuthorizationBoundary.model_validate(value["body"])
    return value


def validate(connection, access, operation, now):
    execution = (
        row(connection, executions, "execution_id", access.execution_id)
        if access.kind == "run"
        else None
    )
    if execution and execution.get("job_id"):
        # Queue leases, deadlines and grant expiry share the database clock, including publication.
        now = connection.scalar(sa.select(sa.func.clock_timestamp()))
    task = row(connection, tasks, "task_id", access.task_id)
    if task is None or task["revision_id"] != access.revision_id or task["epoch"] != access.epoch:
        raise AccessDenied("stale_execution_or_revision")
    if task["state"] in {
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "PAUSED",
        "FAILED",
        "CANCELLED",
        "COMPLETED",
    }:
        raise AccessDenied("task_not_executable")
    revision = checked_revision(connection, task["revision_id"])
    plan = Plan.model_validate(revision["body"])
    grant = checked_grant(connection, task["grant_id"])
    if not active_grant(grant, now) or task["grant_id"] != access.grant_id:
        raise AccessDenied("authorization_not_active")
    if grant["task_id"] != task["task_id"] or not within(plan, grant["body"]):
        raise AccessDenied("plan_exceeds_authorization")
    if (
        revision["frozen_time"] != grant["body"]["time"]
        or operation not in grant["body"]["operations"]
    ):
        raise AccessDenied("operation_or_time_not_authorized")
    # Every ancestor remains a live authorization boundary, even if a child has its own grant.
    seen = {task["task_id"]}
    child = task
    while child["parent_id"]:
        parent = row(connection, tasks, "task_id", child["parent_id"])
        if parent is None or parent["task_id"] in seen or len(seen) > 8:
            raise AccessDenied("invalid_parent_lineage")
        seen.add(parent["task_id"])
        if (
            parent["owner_id"] != task["owner_id"]
            or parent["revision_id"] != child["parent_revision_id"]
            or parent["grant_id"] != child["parent_grant_id"]
            or parent["epoch"] != child["parent_epoch"]
            or parent["state"] not in {"DRAFT", "RUNNING"}
        ):
            raise AccessDenied("parent_authorization_changed")
        parent_grant = checked_grant(connection, parent["grant_id"])
        if (
            not active_grant(parent_grant, now)
            or not within(plan, parent_grant["body"])
            or operation not in parent_grant["body"]["operations"]
        ):
            raise AccessDenied("subtask_exceeds_parent_authorization")
        child = parent
    if access.kind == "run":
        if (
            task["state"] != "RUNNING"
            or task["execution_id"] != access.execution_id
            or execution is None
            or execution["state"] != "running"
            or execution["task_id"] != task["task_id"]
            or execution["revision_id"] != task["revision_id"]
            or execution["grant_id"] != task["grant_id"]
            or execution["epoch"] != access.epoch
            or access.secret is None
            or hashlib.sha256(access.secret.encode()).hexdigest() != execution["permit_sha256"]
        ):
            raise AccessDenied("invalid_execution_permit")
        if execution.get("job_id"):
            from control.queue_schema import jobs

            job = row(connection, jobs, "job_id", execution["job_id"])
            if (
                job is None
                or job["state"] != "running"
                or job["execution_id"] != access.execution_id
                or execution["lease_until"] is None
                or execution["lease_until"] <= now
                or job["deadline"] <= now
                or execution["claim_version"] != task["version"]
            ):
                raise AccessDenied("dispatch_lease_version_or_deadline_expired")
    elif operation != "research" or access.kind != "research" or task["state"] != "DRAFT":
        raise AccessDenied("execution_phase_required")
    return task, revision, grant


def gate(connection, binding_id, operation, *, now=None, request=None):
    revision = row(connection, revisions, "binding_task_id", binding_id)
    if revision is None:
        if (
            binding_id.startswith("CT-")
            or row(connection, tasks, "task_id", binding_id) is not None
        ):
            raise AccessDenied("reserved_control_binding")
        return  # Unmanaged historical development data is not exposed by the protected API.
    access = CURRENT.get()
    if (
        access is None
        or access.task_id != revision["task_id"]
        or access.revision_id != revision["revision_id"]
    ):
        raise AccessDenied("managed_task_requires_service_capability")
    if operation == "bind" and access.kind == "binding":
        revision = checked_revision(connection, access.revision_id)
        task = row(connection, tasks, "task_id", access.task_id)
        plan = Plan.model_validate(revision["body"])
        if task is None or task["revision_id"] != access.revision_id:
            raise AccessDenied("stale_binding")
        if (
            request is None
            or request.release_id != plan.release_id
            or request.scope != plan.scope
            or request.allowed_source_ids != plan.allowed_source_ids
        ):
            raise AccessDenied("binding_differs_from_plan")
        return
    if operation == "close" and access.kind == "close":
        task = row(connection, tasks, "task_id", access.task_id)
        if task and task["state"] in {"COMPLETED", "CANCELLED", "FAILED"}:
            return
        raise AccessDenied("task_not_terminal")
    validate(connection, access, operation, now or datetime.now(UTC))


def checkpoint(catalog, binding_id, operation):
    # A fresh READ COMMITTED transaction sees revocation committed during a long tool/model call.
    with catalog.engine.begin() as connection:
        gate(connection, binding_id, operation)

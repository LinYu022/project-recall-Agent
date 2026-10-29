"""Authenticated local workflow API; model/document text has no credential channel."""

import hashlib
import secrets
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from control.access import AccessDenied, ControlConflict
from control.contracts import Approval, InputAnswer, Research, RevisionInput, Submission, Versioned
from control.queue_contracts import Enqueue, Reconcile
from releases.catalog import ReleaseConflict, ReleaseUnavailable
from runtime.artifacts import ArtifactIntegrityError


def create_control_app(control, token_owners: dict[str, str]):
    if (
        not token_owners
        or any(len(t) < 24 for t in token_owners)
        or any(not isinstance(o, str) or not o.strip() for o in token_owners.values())
    ):
        raise ValueError("configure_long_local_user_tokens")
    # Raw bearer credentials are never put in plans, database rows, evidence or model prompts.
    identities = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in token_owners.items()}
    app = FastAPI(title="授权、计划与任务状态", version="0.1.0")

    def identity(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            raise AccessDenied("user_authentication_required")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        for key, owner in identities.items():
            if secrets.compare_digest(digest, key):
                return owner
        raise AccessDenied("invalid_user_token")

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_request",
                "errors": [{"loc": e["loc"], "type": e["type"]} for e in error.errors()],
            },
        )

    for kind, status, label in [
        (AccessDenied, 403, "access_denied"),
        (PermissionError, 403, "outside_scope"),
        (ControlConflict, 409, "control_conflict"),
        (ReleaseConflict, 409, "release_conflict"),
        (ReleaseUnavailable, 409, "release_unavailable"),
        (ArtifactIntegrityError, 409, "integrity_failed"),
        (ValueError, 422, "invalid_request"),
        (OSError, 503, "component_unavailable"),
        (sa.exc.SQLAlchemyError, 503, "database_unavailable"),
    ]:

        def handler_factory(code, name):
            async def handler(request: Request, error: Exception):
                return JSONResponse(status_code=code, content={"status": name})

            return handler

        app.add_exception_handler(kind, handler_factory(status, label))

    @app.post("/tasks")
    def submit(value: Submission, owner=Depends(identity)):
        return control.submit(owner, value)

    @app.get("/tasks/{task_id}")
    def view(task_id: str, owner=Depends(identity)):
        return control.view(owner, task_id)

    @app.post("/tasks/{task_id}/revisions")
    def revise(task_id: str, value: RevisionInput, owner=Depends(identity)):
        return control.revise(owner, task_id, value.expected_version, value.plan)

    @app.post("/tasks/{task_id}/answers")
    def answer(task_id: str, value: InputAnswer, owner=Depends(identity)):
        return control.answer(owner, task_id, value)

    @app.post("/tasks/{task_id}/approvals")
    def approval(task_id: str, value: Approval, owner=Depends(identity)):
        return control.approve(owner, task_id, value)

    @app.post("/tasks/{task_id}/research")
    def research(task_id: str, value: Research, owner=Depends(identity)):
        return control.research(owner, task_id, value)

    @app.post("/tasks/{task_id}/execute")
    def execute(task_id: str, value: Versioned, owner=Depends(identity)):
        if control.dispatcher is not None:
            return control.dispatcher.enqueue(
                owner,
                task_id,
                Enqueue(
                    expected_version=value.expected_version,
                    idempotency_key=f"execute-{value.expected_version}",
                ),
            )
        return control.execute(owner, task_id, value.expected_version)

    @app.post("/tasks/{task_id}/dispatch")
    def dispatch(task_id: str, value: Enqueue, owner=Depends(identity)):
        if control.dispatcher is None:
            raise ValueError("dispatcher_not_configured")
        return control.dispatcher.enqueue(owner, task_id, value)

    @app.get("/tasks/{task_id}/dispatch")
    def dispatch_view(task_id: str, owner=Depends(identity)):
        if control.dispatcher is None:
            raise ValueError("dispatcher_not_configured")
        return control.dispatcher.view(owner, task_id)

    @app.post("/tasks/{task_id}/recover")
    def recover(task_id: str, value: Versioned, owner=Depends(identity)):
        if control.dispatcher is None:
            raise ValueError("dispatcher_not_configured")
        return control.dispatcher.recover(owner, task_id, value.expected_version)

    @app.post("/tasks/{task_id}/reconcile")
    def reconcile(task_id: str, value: Reconcile, owner=Depends(identity)):
        if control.dispatcher is None:
            raise ValueError("dispatcher_not_configured")
        return control.dispatcher.reconcile(owner, task_id, value)

    for action in ("pause", "resume", "cancel", "revoke"):

        def action_factory(name):
            def operation(task_id: str, value: Versioned, owner=Depends(identity)):
                return control.change_state(owner, task_id, value.expected_version, name)

            return operation

        app.post("/tasks/{task_id}/" + action)(action_factory(action))

    @app.get("/tasks/{task_id}/report")
    def report(task_id: str, owner=Depends(identity)):
        return control.report(owner, task_id)

    @app.get("/tasks/{task_id}/history")
    def history(task_id: str, owner=Depends(identity)):
        return control.history(owner, task_id)

    @app.get("/tasks/{task_id}/context")
    def context_page(
        task_id: str,
        section: str = "observations",
        alias: str | None = None,
        artifact_id: str | None = None,
        offset: int = 0,
        limit: int = 2000,
        owner=Depends(identity),
    ):
        return control.inspect(
            owner,
            task_id,
            section=section,
            alias=alias,
            artifact_id=artifact_id,
            offset=offset,
            limit=limit,
        )

    from app.memory_api import routes

    routes(app, control, identity)
    from app.episode_api import routes as episode_routes

    episode_routes(app, control, identity)
    from app.procedure_api import routes as procedure_routes

    procedure_routes(app, control, identity)
    return app

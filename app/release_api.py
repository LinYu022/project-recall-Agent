"""Canonical task-bound published query API; administration remains local CLI only."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.development import restrict_legacy
from database.config import database_url
from domain.tasks import Contract
from entities.scopes import RecordLocator
from ingestion.contracts import Digest
from releases.bundle import BundleStore
from releases.catalog import Catalog, ReleaseConflict, ReleaseUnavailable
from releases.contracts import BindTask, ReleaseQuery
from releases.service import PublishedService
from runtime.artifacts import ArtifactIntegrityError


class Expansion(Contract):
    request: ReleaseQuery
    document_id: Digest
    include_related: bool = False


class CountInput(Contract):
    request: ReleaseQuery
    components: list[str] = []


class LinkInput(Contract):
    request: ReleaseQuery
    left: RecordLocator
    right: RecordLocator


def create_release_app(
    *, service: PublishedService | None = None, investigator=None, development_mode=False
) -> FastAPI:
    engine = None
    if service is None:
        engine = sa.create_engine(
            database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
        )
        service = PublishedService(
            Catalog(
                engine, BundleStore(Path(os.environ.get("RECALL_RELEASE_ROOT", ".local/releases")))
            ),
            Path(".local/releases/traces"),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    app = FastAPI(title="Published evidence queries", version="0.1.0", lifespan=lifespan)

    # Production workflow uses control_api. Historical unsecured endpoints require an explicit
    # local development opt-in, including when accessed through a hand-crafted HTTP request.
    restrict_legacy(app, ("/tasks", "/published"), development_mode)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_request",
                "errors": [{"loc": e["loc"], "type": e["type"]} for e in error.errors()],
            },
        )

    for kind, status, name in [
        (ReleaseConflict, 409, "release_conflict"),
        (ReleaseUnavailable, 409, "release_unavailable"),
        (ArtifactIntegrityError, 409, "release_integrity_failed"),
        (PermissionError, 403, "outside_scope"),
        (OSError, 503, "release_component_unavailable"),
        (ValueError, 422, "invalid_release_request"),
        (sa.exc.SQLAlchemyError, 503, "database_unavailable"),
    ]:

        def handler_factory(code, label):
            async def handler(request: Request, error: Exception):
                return JSONResponse(status_code=code, content={"status": label})

            return handler

        app.add_exception_handler(kind, handler_factory(status, name))

    @app.post("/tasks/bind")
    def bind(request: BindTask):
        assert service is not None
        result = service.catalog.bind(request)
        return {
            "status": "bound",
            "task_id": result["task_id"],
            "release_id": result["release_id"],
            "spec_sha256": result["spec_sha256"],
        }

    @app.post("/published/search")
    def search(request: ReleaseQuery):
        assert service is not None
        return service.query(request)

    @app.post("/published/expand")
    def expand(request: Expansion):
        assert service is not None
        return service.query(
            request.request,
            document_id=request.document_id,
            include_related=request.include_related,
        )

    @app.post("/published/complaints/count")
    def count(request: CountInput):
        assert service is not None
        return service.count(request.request, request.components)

    @app.post("/published/entity-links")
    def links(request: LinkInput):
        assert service is not None
        return service.links(request.request, request.left, request.right)

    # Explicit injection prevents an existing query-only server from gaining billable calls.
    if investigator is not None:
        from investigation.contracts import InvestigationRequest

        @app.post("/published/investigate")
        def investigate(request: InvestigationRequest):
            return investigator.run(request)

    return app

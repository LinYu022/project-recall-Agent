"""Opt-in local query API for saved sources; no fetch, import, mutation, or model routes."""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from app.development import restrict_legacy
from database.config import database_url
from domain.tasks import Contract
from entities.counts import ComplaintCountRequest, count_complaints
from entities.errors import ProjectionNotReady, QueryDatabaseUnavailable
from entities.normalize import EntityRules, load_rules
from entities.query import TemporalLinkRequest, query_links
from entities.scopes import ScopeRules, load_scope_rules
from ingestion.contracts import SnapshotID
from ingestion.snapshots import SnapshotStore
from runtime.artifacts import ArtifactIntegrityError


class QuerySettings(Contract):
    schema_version: Literal["query-settings/0.1"] = "query-settings/0.1"
    snapshot_root: Path
    allowed_snapshot_ids: Annotated[list[SnapshotID], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.allowed_snapshot_ids)) != len(self.allowed_snapshot_ids):
            raise ValueError("query snapshot allowlist must be unique")
        return self


@dataclass
class QueryServices:
    settings: QuerySettings
    engine: sa.Engine | None
    count_rules: EntityRules
    scope_rules: ScopeRules


def get_query_services(request: Request) -> QueryServices:
    return request.app.state.query_services


InjectedQueries = Annotated[QueryServices, Depends(get_query_services)]
QUERY_RESPONSES: dict[int | str, dict] = {
    200: {"description": "Query evaluated; inspect status for partial/unsupported results"},
    403: {"description": "Snapshot is outside the server selection"},
    404: {"description": "Registered snapshot receipt is not present"},
    409: {"description": "Source integrity or entity projection conflict"},
    422: {"description": "Invalid request or unsupported exact query parameters"},
    500: {"description": "Local source storage could not be read"},
    503: {"description": "Query database is not available"},
}


def _check_inputs(services: QueryServices, snapshot_ids: list[str]) -> None:
    if not set(snapshot_ids) <= set(services.settings.allowed_snapshot_ids):
        raise HTTPException(status_code=403, detail="snapshot_not_in_server_selection")
    store = SnapshotStore(services.settings.snapshot_root)
    for identifier in snapshot_ids:
        if not (store.root / "snapshots" / (identifier + ".json")).is_file():
            raise HTTPException(status_code=404, detail="snapshot_not_found")
        try:
            store.verify(identifier)
        except FileNotFoundError:
            raise ArtifactIntegrityError("snapshot body is missing") from None
        except ValueError:
            raise ArtifactIntegrityError("snapshot manifest or source body is invalid") from None


def create_query_app(
    settings: QuerySettings | None = None,
    *,
    engine: sa.Engine | None = None,
    count_rules: EntityRules | None = None,
    scope_rules: ScopeRules | None = None,
    development_mode=False,
) -> FastAPI:
    if settings is None:
        path = Path(os.environ.get("RECALL_QUERY_CONFIG", "examples/query-readonly.json"))
        settings = QuerySettings.model_validate_json(path.read_bytes())
    configured_engine = engine
    owned_engine = False
    if configured_engine is None and os.environ.get("RECALL_DATABASE_URL"):
        configured_engine = sa.create_engine(
            database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
        )
        owned_engine = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if owned_engine and configured_engine is not None:
                configured_engine.dispose()

    app = FastAPI(title="Saved recall evidence query API", version="0.1.0", lifespan=lifespan)
    restrict_legacy(app, ("/queries",), development_mode)
    app.state.query_services = QueryServices(
        settings, configured_engine, count_rules or load_rules(), scope_rules or load_scope_rules()
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_request",
                "errors": [{"type": e["type"], "loc": e["loc"]} for e in error.errors()],
            },
        )

    @app.exception_handler(ArtifactIntegrityError)
    async def integrity_error(request: Request, error: ArtifactIntegrityError):
        return JSONResponse(status_code=409, content={"status": "integrity_error"})

    @app.exception_handler(ProjectionNotReady)
    async def missing_projection(request: Request, error: ProjectionNotReady):
        return JSONResponse(status_code=409, content={"status": "projection_not_ready"})

    @app.exception_handler(QueryDatabaseUnavailable)
    @app.exception_handler(sa.exc.SQLAlchemyError)
    async def unavailable_database(request: Request, error: Exception):
        return JSONResponse(status_code=503, content={"status": "query_database_unavailable"})

    @app.exception_handler(ValueError)
    async def invalid_query(request: Request, error: ValueError):
        return JSONResponse(status_code=422, content={"status": "invalid_query"})

    @app.exception_handler(OSError)
    async def io_error(request: Request, error: OSError):
        return JSONResponse(status_code=500, content={"status": "source_io_error"})

    @app.get("/health")
    def health(services: InjectedQueries) -> dict:
        return {
            "status": "ok",
            "data_mode": "saved_source_snapshots",
            "model_provider": None,
            "dataset_release": None,
            "database_configured": services.engine is not None,
            "database_connection_checked": False,
            "allowed_snapshots": len(services.settings.allowed_snapshot_ids),
        }

    @app.post("/queries/complaints/count", responses=QUERY_RESPONSES)
    def complaints(body: ComplaintCountRequest, services: InjectedQueries) -> dict:
        _check_inputs(services, body.temporal.snapshot_ids)
        return count_complaints(
            services.engine, services.settings.snapshot_root, body, services.count_rules
        )

    @app.post("/queries/entity-links", responses=QUERY_RESPONSES)
    def links(body: TemporalLinkRequest, services: InjectedQueries) -> dict:
        _check_inputs(services, body.temporal.snapshot_ids)
        return query_links(services.settings.snapshot_root, body, services.scope_rules)

    return app

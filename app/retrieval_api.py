"""Local retrieval API; fixed server index/models and source selection, trace writes only."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field

from app.development import restrict_legacy
from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from retrieval.contracts import SearchRequest
from retrieval.index import checked_index
from retrieval.models import LocalModel
from retrieval.service import ModelsUnavailable, RetrievalService
from retrieval.vector_store import QdrantVectors
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


class RetrievalSettings(Contract):
    schema_version: Literal["retrieval-settings/0.1"] = "retrieval-settings/0.1"
    index_artifact_sha256: Digest
    index_root: Path = Path(".local/retrieval/indexes")
    qdrant_root: Path = Path(".local/retrieval/qdrant")
    models_root: Path = Path(".local/retrieval/models")
    trace_root: Path = Path(".local/retrieval/traces")
    raw_root: Path = Path(".local/source-snapshots")
    parsed_root: Path = Path(".local/parsed-sources")
    chunk_root: Path = Path(".local/chunk-sets")
    allowed_source_ids: Annotated[list[Text], Field(min_length=1)]


class ExpandRequest(Contract):
    search: SearchRequest
    document_id: Digest
    include_related: bool = False


def create_retrieval_app(
    settings: RetrievalSettings | None = None,
    *,
    service: RetrievalService | None = None,
    development_mode=False,
) -> FastAPI:
    if settings is None:
        path = Path(os.environ.get("RECALL_RETRIEVAL_CONFIG", ".local/retrieval/settings.json"))
        settings = RetrievalSettings.model_validate_json(path.read_bytes())
    owned_vectors = None
    if service is None:
        index = checked_index(settings.index_root, settings.index_artifact_sha256)
        owned_vectors = QdrantVectors(index, settings.index_artifact_sha256, settings.qdrant_root)
        service = RetrievalService(
            index,
            encoder=LocalModel("encoder", settings.models_root),
            reranker=LocalModel("reranker", settings.models_root),
            allowed_source_ids=settings.allowed_source_ids,
            vector_store=owned_vectors,
        )
    if service.allowed_sources != set(settings.allowed_source_ids):
        raise ValueError("injected service and server source selections differ")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if owned_vectors is not None:
                owned_vectors.close()

    app = FastAPI(title="Pinned evidence retrieval", version="0.1.0", lifespan=lifespan)
    restrict_legacy(app, ("/retrieval",), development_mode)
    lock = RLock()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_request",
                "errors": [{"type": e["type"], "loc": e["loc"]} for e in error.errors()],
            },
        )

    @app.exception_handler(PermissionError)
    async def outside_scope(request: Request, error: PermissionError):
        return JSONResponse(status_code=403, content={"status": "outside_scope"})

    @app.exception_handler(ArtifactIntegrityError)
    async def integrity(request: Request, error: ArtifactIntegrityError):
        return JSONResponse(status_code=409, content={"status": "artifact_integrity_failed"})

    @app.exception_handler(ModelsUnavailable)
    async def unavailable(request: Request, error: ModelsUnavailable):
        return JSONResponse(status_code=503, content={"status": "model_unavailable"})

    @app.exception_handler(ValueError)
    async def bad_query(request: Request, error: ValueError):
        return JSONResponse(status_code=422, content={"status": "invalid_retrieval_request"})

    @app.exception_handler(OSError)
    async def io_error(request: Request, error: OSError):
        return JSONResponse(status_code=503, content={"status": "local_storage_unavailable"})

    @app.post("/retrieval/search")
    def search(request: SearchRequest):
        assert service is not None and settings is not None
        with lock:
            result = service.search(request)
            ref = ArtifactStore(settings.trace_root).put_json(result)
        return result | {
            "trace_artifact": ref,
            "index_artifact_sha256": settings.index_artifact_sha256,
        }

    @app.post("/retrieval/expand")
    def expand(request: ExpandRequest):
        assert service is not None and settings is not None
        with lock:
            result = service.expand(
                request.search,
                request.document_id,
                raw_root=settings.raw_root,
                parsed_root=settings.parsed_root,
                chunk_root=settings.chunk_root,
                include_related=request.include_related,
            )
            ref = ArtifactStore(settings.trace_root).put_json(result)
        return result | {"trace_artifact": ref}

    return app

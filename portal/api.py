"""Same control service and versioned writes, with authenticated product read views."""

import hashlib
import secrets
from pathlib import Path
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.control_api import create_control_app
from control.access import AccessDenied
from control.schema import tasks
from database.schema import dataset_releases
from investigation.contracts import EvidenceCard
from memory import schema as memory_db
from parsing.citations import resolve_citation
from parsing.contracts import Citation


def create_product_app(control, token_owners, *, profile, ui_root=None, build_identity=None):
    app = create_control_app(control, token_owners)
    hashes = {hashlib.sha256(t.encode()).hexdigest(): owner for t, owner in token_owners.items()}

    def identity(authorization: Annotated[str | None, Header()] = None):
        if authorization and authorization.startswith("Bearer "):
            actual = hashlib.sha256(authorization[7:].encode()).hexdigest()
            for expected, owner in hashes.items():
                if secrets.compare_digest(actual, expected):
                    return owner
        raise AccessDenied("user_authentication_required")

    @app.middleware("http")
    async def product_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            return JSONResponse(status_code=403, content={"status": "same_origin_required"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/product/info")
    def info():
        return {
            "profile": profile,
            "is_simulated_generation": profile != "live",
            "model_quality_acceptance": "not_established",
            "default_investigator": "fixed",
            "external_calls_in_demo": 0,
            "implementation_sha256": build_identity,
        }

    @app.get("/product/catalog")
    def catalog(owner=Depends(identity)):
        result = []
        with control.catalog.transaction(shared=True) as connection:
            rows = connection.execute(
                sa.select(dataset_releases).where(dataset_releases.c.state == "available")
            ).mappings()
            for row in rows:
                _, manifest = control.catalog._available(connection, row["release_id"])
                result.append(
                    {
                        "release_id": row["release_id"],
                        "source_ids": manifest["allowed_source_ids"],
                        "synthetic": manifest["test_only"],
                    }
                )
        return {"releases": result}

    @app.get("/product/tasks")
    def task_list(
        offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), owner=Depends(identity)
    ):
        with control.catalog.engine.begin() as connection:
            rows = connection.execute(
                sa.select(tasks.c.task_id, tasks.c.state, tasks.c.version, tasks.c.quality)
                .where(tasks.c.owner_id == owner)
                .order_by(tasks.c.task_id)
                .offset(offset)
                .limit(limit)
            ).mappings()
            return {"tasks": [dict(row) for row in rows], "offset": offset, "limit": limit}

    @app.get("/product/memories")
    def memories(owner=Depends(identity)):
        with control.catalog.transaction() as connection:
            control.memory._expire(connection, owner)
            rows = connection.execute(
                sa.select(memory_db.items.c.memory_id)
                .where(memory_db.items.c.owner_id == owner)
                .order_by(memory_db.items.c.memory_id)
                .limit(100)
            ).scalars()
            ids = list(rows)
        return {"items": [control.memory.item(owner, key) for key in ids]}

    @app.get("/product/tasks/{task_id}/original/{alias}")
    def original(task_id: str, alias: str, owner=Depends(identity)):
        # Same transaction as report disclosure. No client path, URL or raw digest is accepted.
        with control.catalog.transaction() as connection:
            _, report = control._published_report(connection, owner, task_id)
            if alias not in report["aliases"]:
                raise HTTPException(404, "unknown_evidence_alias")
            card = EvidenceCard.model_validate(
                next(c for c in report["cards"] if c["evidence_id"] == report["aliases"][alias])
            )
            if card.release_id != report["release_id"] or not set(card.source_ids) <= set(
                report["allowed_source_ids"]
            ):
                raise AccessDenied("evidence_outside_report")
            _, manifest = control.catalog._available(connection, report["release_id"])
            base = control.catalog.store.directory(manifest)
            citations = card.payload.get("citations", [])
            return {
                "alias": alias,
                "text": card.payload.get("text"),
                "locations": [
                    resolve_citation(base / "raw", base / "parsed", Citation.model_validate(c))
                    | {"quote": c["quote"]}
                    for c in citations
                ],
                "verification": "integrity_and_text_replay_only_not_semantic_acceptance",
            }

    @app.get("/product/memories/{memory_id}/source")
    def memory_source(memory_id: str, owner=Depends(identity)):
        item = control.memory.item(owner, memory_id)
        with control.catalog.transaction() as connection:
            candidate = control.memory._owned(
                connection, memory_db.candidates, "candidate_id", item["candidate_id"], owner
            )
            source = control.memory._source(connection, owner, candidate["source_id"])
        return {"memory_id": memory_id, "source": source}

    @app.get("/product/tasks/{task_id}/evidence")
    def evidence(task_id: str, owner=Depends(identity)):
        # Reuse the report gate, including revocation and release availability checks.
        report = control.report(owner, task_id)
        return {"cards": report["cards"], "aliases": report["aliases"]}

    if ui_root is not None:
        root = Path(ui_root).resolve()
        if not (root / "index.html").is_file():
            raise ValueError("build_frontend_first")
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(root / "index.html")

    return app

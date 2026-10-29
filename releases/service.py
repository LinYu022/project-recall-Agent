"""Every published query inherits its immutable task release and verifies all components."""

from pathlib import Path
from threading import RLock
from time import perf_counter

from domain.tasks import EventInterval
from entities.counts import ComplaintCountRequest, count_complaints
from entities.normalize import EntityRules
from entities.query import TemporalLinkRequest, query_links
from entities.scopes import RecordLocator, ScopeRules
from ingestion.temporal import TemporalRequest
from releases.bundle import read_json
from releases.catalog import Catalog
from releases.contracts import ReleaseQuery
from retrieval.contracts import SearchRequest
from retrieval.index import checked_index
from retrieval.models import LocalModel
from retrieval.scope import classify_scope
from retrieval.service import RetrievalService
from retrieval.vector_store import QdrantVectors
from runtime.artifacts import ArtifactStore


class PublishedService:
    def __init__(self, catalog: Catalog, trace_root: Path, *, model_factory=None):
        self.catalog, self.trace_root = catalog, trace_root
        self.model_factory = model_factory
        self.lock = RLock()

    def fork(self):
        # Independent operation locks; QdrantVectors separately coordinates all local
        # directory access, including the integrity verification done by SQL queries.
        from copy import copy

        sibling = copy(self)
        sibling.lock = RLock()
        return sibling

    def model(self, role, base, identities):
        result = (
            self.model_factory(role)
            if self.model_factory
            else LocalModel(role, base / "models", base / "models/manifest.json")
        )
        if result.identity != identities[role]:
            raise ValueError("runtime model differs from release identity")
        return result

    def query(self, request: ReleaseQuery, *, document_id=None, include_related=False):
        from control.access import checkpoint

        checkpoint(self.catalog, request.task_id, "research")
        result = self._query(request, document_id=document_id, include_related=include_related)
        checkpoint(self.catalog, request.task_id, "research")
        return result

    def _query(self, request: ReleaseQuery, *, document_id=None, include_related=False):
        started = perf_counter()
        with self.lock, self.catalog.transaction(shared=True) as connection:
            task, manifest = self.catalog.task(
                connection, request.task_id, request.expected_release_id
            )
            base = self.catalog.store.directory(manifest)
            index = checked_index(base / "index", manifest["index_artifact_sha256"])
            vector_store = QdrantVectors(
                index, manifest["index_artifact_sha256"], base / "qdrant", compact=True
            )
            try:
                identities = read_json(base / "models/identities.json")
                encoder = (
                    self.model("encoder", base, identities)
                    if document_id is None and request.method != "bm25"
                    else None
                )
                reranker = (
                    self.model("reranker", base, identities)
                    if document_id is None and request.method == "hybrid_rerank"
                    else None
                )
                service = RetrievalService(
                    index,
                    encoder=encoder,
                    reranker=reranker,
                    vector_store=vector_store,
                    allowed_source_ids=task["spec"]["allowed_source_ids"],
                )
                search = SearchRequest(
                    dataset_build_id=manifest["dataset_build_id"],
                    query=request.query,
                    method=request.method,
                    top_k=request.top_k,
                    allowed_source_ids=task["spec"]["allowed_source_ids"],
                    scope=task["spec"]["scope"],
                )
                result = (
                    service.search(search)
                    if document_id is None
                    else service.expand(
                        search,
                        document_id,
                        raw_root=base / "raw",
                        parsed_root=base / "parsed",
                        chunk_root=base / "chunks",
                        include_related=include_related,
                    )
                )
            finally:
                vector_store.close()
            result = result | {
                "dataset_release": manifest["release_id"],
                "release_id": manifest["release_id"],
                "task_id": request.task_id,
                "manifest_sha256": task["manifest_sha256"],
                "published_elapsed_ms": (perf_counter() - started) * 1000,
            }
            ref = ArtifactStore(self.trace_root).put_json(result)
            return result | {"trace_artifact": ref}

    def count(self, request: ReleaseQuery, components: list[str]):
        from control.access import checkpoint

        checkpoint(self.catalog, request.task_id, "research")
        result = self._count(request, components)
        checkpoint(self.catalog, request.task_id, "research")
        return result

    def _count(self, request: ReleaseQuery, components: list[str]):
        with self.lock, self.catalog.transaction(shared=True) as connection:
            task, manifest = self.catalog.task(
                connection, request.task_id, request.expected_release_id
            )
            base = self.catalog.store.directory(manifest)
            scope = task["spec"]["scope"]
            if any(scope.get(k) is None for k in ("make", "model", "model_year")):
                raise ValueError("count requires pinned vehicle scope")
            if scope.get("configurations") or scope.get("campaign_number"):
                return {
                    "status": "unsupported",
                    "reason": "count_does_not_support_pinned_configuration_or_campaign",
                    "distinct_complaints": None,
                    "dataset_release": manifest["release_id"],
                    "release_id": manifest["release_id"],
                    "task_id": request.task_id,
                }
            if scope.get("component"):
                if components and set(components) != {scope["component"]}:
                    raise PermissionError("count components would widen pinned task scope")
                components = [scope["component"]]
            corpus = checked_index(base / "index", manifest["index_artifact_sha256"])["corpus"]
            selected = [
                sid
                for sid in manifest["snapshot_ids"]
                if read_json(base / f"raw/snapshots/{sid}.json")["source"]["source_id"]
                in task["spec"]["allowed_source_ids"]
            ]
            temporal = TemporalRequest(
                snapshot_ids=selected, time=corpus["recipe"]["request"]["time"]
            )
            query = ComplaintCountRequest(
                temporal=temporal,
                make=scope["make"],
                model=scope["model"],
                model_year=scope["model_year"],
                components=components,
                production_interval=EventInterval.model_validate(
                    {
                        "start": scope["production_date"],
                        "end": scope["production_date"],
                    }
                )
                if scope.get("production_date")
                else None,
            )
            result = count_complaints(
                self.catalog.engine,
                base / "raw",
                query,
                EntityRules.model_validate(read_json(base / "rules/entity.json")),
            )
            return result | {
                "dataset_release": manifest["release_id"],
                "release_id": manifest["release_id"],
                "task_id": request.task_id,
            }

    def links(self, request: ReleaseQuery, left: RecordLocator, right: RecordLocator):
        from control.access import checkpoint

        checkpoint(self.catalog, request.task_id, "research")
        result = self._links(request, left, right)
        checkpoint(self.catalog, request.task_id, "research")
        return result

    def _links(self, request: ReleaseQuery, left: RecordLocator, right: RecordLocator):
        with self.lock, self.catalog.transaction(shared=True) as connection:
            task, manifest = self.catalog.task(
                connection, request.task_id, request.expected_release_id
            )
            base = self.catalog.store.directory(manifest)
            corpus = checked_index(base / "index", manifest["index_artifact_sha256"])["corpus"]
            selected = [
                sid
                for sid in manifest["snapshot_ids"]
                if read_json(base / f"raw/snapshots/{sid}.json")["source"]["source_id"]
                in task["spec"]["allowed_source_ids"]
            ]
            if not {left.snapshot_id, right.snapshot_id} <= set(selected):
                raise PermissionError("relationship endpoints outside task source selection")
            temporal = TemporalRequest(
                snapshot_ids=selected, time=corpus["recipe"]["request"]["time"]
            )
            result = query_links(
                base / "raw",
                TemporalLinkRequest(temporal=temporal, left=left, right=right),
                ScopeRules.model_validate(read_json(base / "rules/scope.json")),
            )
            channels = []
            for locator in (left, right):
                doc = next(
                    (
                        d
                        for d in corpus["documents"]
                        if d["source"]["snapshot_id"] == locator.snapshot_id
                        and d["source"]["ordinal"] == locator.ordinal
                    ),
                    None,
                )
                from retrieval.contracts import SearchScope

                channels.append(
                    classify_scope(
                        doc["metadata"], SearchScope.model_validate(task["spec"]["scope"])
                    )[0]
                    if doc
                    else "not_selected"
                )
            return result | {
                "dataset_release": manifest["release_id"],
                "release_id": manifest["release_id"],
                "task_id": request.task_id,
                "endpoint_scope_channels": channels,
                "use": "relationship_inspection_not_applicability_evidence",
            }

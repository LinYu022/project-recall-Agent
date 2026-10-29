"""Shared filtering, candidate traces, four ranking paths and permission-bound expansion."""

from pathlib import Path
from time import perf_counter

import numpy as np

from ingestion.records import content_hash
from parsing.chunks import checked_chunks, cite_chunk, expand_chunk
from retrieval.contracts import RetrievalConfig, SearchRequest
from retrieval.models import Encoder, Reranker
from retrieval.ranking import bm25_scores, ranked, rrf
from retrieval.scope import classify_scope
from retrieval.vector_store import NumpyVectors


class ModelsUnavailable(RuntimeError):
    pass


class RetrievalService:
    def __init__(
        self,
        index: dict,
        *,
        encoder: Encoder | None = None,
        reranker: Reranker | None = None,
        allowed_source_ids: list[str] | None = None,
        vector_store=None,
    ):
        self.index = index
        self.documents = index["corpus"]["documents"]
        self.identifiers = [d["document_id"] for d in self.documents]
        self.vectors = np.asarray(index["vectors"], dtype=np.float32)
        self.config = RetrievalConfig.model_validate(index["config"])
        available = {d["metadata"]["source_id"] for d in self.documents}
        self.allowed_sources = (
            set(allowed_source_ids) if allowed_source_ids is not None else available
        )
        if not self.allowed_sources <= available:
            raise ValueError("server source selection is not indexed")
        if encoder is not None and encoder.identity != index["encoder"]:
            raise ValueError("query encoder differs from index encoder")
        self.encoder, self.reranker = encoder, reranker
        self.vector_store = vector_store if vector_store is not None else NumpyVectors(index)

    def _scope(self, request: SearchRequest) -> tuple[dict, list]:
        if request.dataset_build_id != self.index["dataset_build_id"]:
            raise ValueError("request build differs from pinned index")
        if not set(request.allowed_source_ids) <= self.allowed_sources:
            raise PermissionError("source outside server selection")
        groups: dict[str, set[int]] = {"matched": set(), "pending": set(), "excluded": set()}
        trace = []
        for i, document in enumerate(self.documents):
            if document["metadata"]["source_id"] not in request.allowed_source_ids:
                continue
            channel, reasons = classify_scope(document["metadata"], request.scope)
            groups[channel].add(i)
            trace.append(
                {"document_id": document["document_id"], "channel": channel, "reasons": reasons}
            )
        return groups, trace

    def search(self, request: SearchRequest) -> dict:
        started = perf_counter()
        groups, filter_trace = self._scope(request)
        if request.top_k > self.config.candidate_k:
            raise ValueError("top_k exceeds candidate limit")
        if request.method == "hybrid_rerank" and request.top_k > self.config.rerank_limit:
            raise ValueError("top_k exceeds reranker input limit")
        active = bool(groups["matched"] or groups["pending"])
        timings, usage, stages = {}, {}, []
        bm25: dict[int, float] = {}
        dense: dict[int, float] = {}
        if active and request.method in {"bm25", "hybrid", "hybrid_rerank"}:
            tick = perf_counter()
            bm25 = bm25_scores(
                self.index["bm25"], request.query, self.config.bm25_k1, self.config.bm25_b
            )
            timings["bm25_ms"] = (perf_counter() - tick) * 1000
        if active and request.method in {"dense", "hybrid", "hybrid_rerank"}:
            if self.encoder is None:
                raise ModelsUnavailable("dense query model is not loaded")
            tick = perf_counter()
            vector, usage["query_encoder"] = self.encoder.encode([request.query])
            norm = np.linalg.norm(vector)
            if (
                vector.shape != (1, self.vectors.shape[1])
                or not np.isfinite(vector).all()
                or norm <= 0
            ):
                raise ValueError("invalid query embedding")
            query_vector = vector[0] / norm
            timings["dense_ms"] = (perf_counter() - tick) * 1000

        def log(stage, channel, entries):
            stages.append(
                {
                    "stage": stage,
                    "channel": channel,
                    "candidates": [
                        {"document_id": self.identifiers[i], "rank": rank, "score": float(score)}
                        for rank, (i, score) in enumerate(entries, 1)
                    ],
                }
            )

        hits = {}
        for channel in ("matched", "pending"):
            pool = groups[channel]
            if active and request.method != "bm25":
                tick = perf_counter()
                dense = self.vector_store.scores(query_vector, pool)
                timings["vector_search_ms:" + channel] = (perf_counter() - tick) * 1000
            canonical: dict[str, int] = {}
            for i in sorted(pool, key=lambda p: self.identifiers[p]):
                canonical.setdefault(self.documents[i]["evidence_key"], i)

            def order(scores, limit):
                collapsed: dict[int, float] = {}
                for i, score in scores.items():
                    if i in pool:
                        representative = canonical[self.documents[i]["evidence_key"]]
                        collapsed[representative] = max(
                            score, collapsed.get(representative, float("-inf"))
                        )
                return ranked(collapsed, pool, self.identifiers, limit)

            sparse = order(bm25, self.config.candidate_k)
            semantic = order(dense, self.config.candidate_k)
            if request.method != "dense":
                log("bm25", channel, sparse)
            if request.method != "bm25":
                log("dense", channel, semantic)
            if request.method == "bm25":
                candidates = sparse
            elif request.method == "dense":
                candidates = semantic
            else:
                tick = perf_counter()
                candidates = order(
                    rrf([sparse, semantic], self.config.rrf_constant), self.config.candidate_k
                )
                timings["fusion_ms:" + channel] = (perf_counter() - tick) * 1000
                log("rrf", channel, candidates)
            if request.method == "hybrid_rerank" and candidates:
                if self.reranker is None:
                    raise ModelsUnavailable("reranker is not loaded; no silent fallback")
                inputs = candidates[: self.config.rerank_limit]
                tick = perf_counter()
                scores, usage["reranker:" + channel] = self.reranker.score(
                    request.query, [self.documents[i]["search_text"] for i, _ in inputs]
                )
                if len(scores) != len(inputs) or not np.isfinite(scores).all():
                    raise ValueError("invalid reranker scores")
                candidates = order(
                    {i: float(score) for (i, _), score in zip(inputs, scores, strict=True)},
                    self.config.rerank_limit,
                )
                timings["rerank_ms:" + channel] = (perf_counter() - tick) * 1000
                log("rerank", channel, candidates)
            hits[channel] = [
                self.documents[i]
                | {
                    "score": float(score),
                    "rank": rank,
                    "scope_channel": channel,
                    "equivalent_document_ids": [
                        self.identifiers[p]
                        for p in sorted(pool)
                        if self.documents[p]["evidence_key"] == self.documents[i]["evidence_key"]
                    ],
                    "applicability": "not_individually_confirmed"
                    if channel == "matched"
                    else "requires_scope_verification",
                }
                for rank, (i, score) in enumerate(candidates[: request.top_k], 1)
            ]
        return {
            "schema_version": "retrieval-result/0.1",
            "dataset_build_id": request.dataset_build_id,
            "index_encoder": self.index["encoder"],
            "vector_backend": self.vector_store.identity,
            "reranker": self.reranker.identity
            if request.method == "hybrid_rerank" and self.reranker
            else None,
            "request": request.model_dump(mode="json"),
            "request_sha256": content_hash(request.model_dump(mode="json")),
            "status": "partial"
            if hits["pending"]
            else "retrieved"
            if hits["matched"]
            else "no_matches",
            "matched_hits": hits["matched"],
            "pending_hits": hits["pending"],
            "scope_trace": filter_trace,
            "candidate_trace": stages,
            "scope_counts": {key: len(value) for key, value in groups.items()},
            "model_usage": usage,
            "timings_ms": timings,
            "total_ms": (perf_counter() - started) * 1000,
            "semantic_support": "not_evaluated",
            "dataset_release": None,
        }

    def expand(
        self,
        request: SearchRequest,
        document_id: str,
        *,
        raw_root: Path,
        parsed_root: Path,
        chunk_root: Path,
        include_related=False,
    ) -> dict:
        groups, _ = self._scope(request)
        eligible = groups["matched"] | groups["pending"]
        target = next((i for i in eligible if self.identifiers[i] == document_id), None)
        if target is None:
            raise PermissionError("document is outside allowed matching/pending scope")

        def expand_one(position):
            doc = self.documents[position]
            chunks = checked_chunks(raw_root, parsed_root, chunk_root, doc["chunk_artifact_sha256"])
            bundle = expand_chunk(chunks, doc["chunk_id"])
            for node in [bundle["focus"], *bundle["context"]]:
                node["citations"] = cite_chunk(raw_root, parsed_root, chunks, node["chunk_id"])
            return {
                "document_id": doc["document_id"],
                "scope_channel": "matched" if position in groups["matched"] else "pending",
                "bundle": bundle,
            }

        related = []
        if include_related:
            references = self.documents[target]["metadata"]["official_references"]
            # Follow only explicit reviewed references and the same request/source boundaries.
            related = [
                expand_one(i)
                for i in sorted(eligible)
                if i != target
                and self.documents[i]["metadata"].get("campaign_number") in references
            ]
        return {
            "schema_version": "retrieval-expansion/0.1",
            "dataset_build_id": request.dataset_build_id,
            "focus": expand_one(target),
            "related": related,
            "relationship_basis": "reviewed_explicit_official_reference_not_causality",
        }

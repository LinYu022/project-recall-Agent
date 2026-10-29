"""Frozen source-unit retrieval comparison; it never changes default method or labels."""

from pathlib import Path
from statistics import mean

from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from releases.bundle import BundleStore
from retrieval.contracts import Method, SearchRequest
from retrieval.index import checked_index
from retrieval.metrics import retrieval_metrics
from retrieval.models import LocalModel
from retrieval.scope import classify_scope
from retrieval.service import RetrievalService
from retrieval.vector_store import QdrantVectors
from runtime.artifacts import ArtifactStore


def benchmark(protocol, label_root, output_root):
    store = BundleStore(Path(".local/releases"))
    manifest = store.manifests.read_json(protocol.manifest_sha256)
    store.verify(protocol.manifest_sha256, protocol.dataset_release_id)
    base = store.directory(manifest)
    index = checked_index(base / "index", manifest["index_artifact_sha256"])
    encoder, reranker = LocalModel("encoder"), LocalModel("reranker")
    vectors = QdrantVectors(
        index, manifest["index_artifact_sha256"], root=base / "qdrant", compact=True
    )
    traces = ArtifactStore(output_root)
    rows = []
    try:
        service = RetrievalService(index, encoder=encoder, reranker=reranker, vector_store=vectors)
        natural = [c for c in protocol.cases if c.case_id.endswith("-FACTS")]
        methods: list[Method] = ["bm25", "dense", "hybrid", "hybrid_rerank"]
        for method in methods:
            for case in natural:
                ref = ReferenceCase.model_validate_json(
                    (label_root / "references" / (case.case_id + ".json")).read_bytes()
                )
                if content_hash(ref.model_dump(mode="json")) != case.reference_sha256:
                    raise ValueError("retrieval_reference_changed")
                versions = {
                    locator.source_version_id for slot in ref.slots for locator in slot.alternatives
                }
                fields = {slot.slot_id for slot in ref.slots}
                qrels = {
                    d["evidence_key"]: 3
                    for d in index["corpus"]["documents"]
                    if d["metadata"]["source_version_id"] in versions
                    and d["field"].lower() in fields
                }
                request = SearchRequest(
                    dataset_build_id=index["dataset_build_id"],
                    query=case.request.retrieval_query,
                    allowed_source_ids=case.sources,
                    scope=case.scope,
                    method=method,
                    top_k=protocol.profile.retrieve_k,
                )
                result = service.search(request)
                keys = [h["evidence_key"] for h in result["matched_hits"]]
                rows.append(
                    {
                        "case_id": case.case_id,
                        "event_cluster_id": case.event_cluster_id,
                        "method": method,
                        "qrels_sha256": content_hash(qrels),
                        "positive_evidence_units": len(qrels),
                        "pending_hits": len(result["pending_hits"]),
                        "metrics": {str(k): retrieval_metrics(keys, qrels, k) for k in [1, 3, 6]},
                        "total_ms": result["total_ms"],
                        "model_usage": result["model_usage"],
                        "trace": traces.put_json(result),
                        "wrong_scope_hits": sum(
                            classify_scope(h["metadata"], case.scope)[0] != "matched"
                            for h in result["matched_hits"]
                        ),
                    }
                )
            print({"retrieval_method": method, "queries": len(natural)}, flush=True)
        summary = {}
        for method in methods:
            selected = [r for r in rows if r["method"] == method]
            summary[method] = {
                "queries": len(selected),
                "positive_label_queries": sum(r["positive_evidence_units"] > 0 for r in selected),
                "wrong_scope_hits": sum(r["wrong_scope_hits"] for r in selected),
                "by_cluster": {
                    group: {
                        name: mean(vals)
                        if (
                            vals := [
                                r["metrics"]["6"][name]
                                for r in selected
                                if r["event_cluster_id"] == group
                                and r["metrics"]["6"][name] is not None
                            ]
                        )
                        else None
                        for name in ["recall", "ndcg"]
                    }
                    for group in sorted({r["event_cluster_id"] for r in selected})
                },
            }
        return {
            "classification": "real_local_ONNX_Qdrant_retrieval_frozen_reference_units_not_answer_quality",
            "protocol_sha256": content_hash(protocol.model_dump(mode="json")),
            "rows": rows,
            "summary": summary,
            "trace_root": traces.directory.parent.as_posix(),
            "new_provider_requests": 0,
            "new_source_requests": 0,
            "default_changed": False,
            "limits": [
                "Evidence units are chunks of the preselected source row fields, not independently reviewed semantic equivalents.",
                "Queries without positive reference units are retained with null Recall/nDCG.",
                "One observed run per query; no latency confidence claim or default tuning.",
            ],
        }
    finally:
        vectors.close()

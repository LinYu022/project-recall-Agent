"""Prelabelled development comparison with real local ONNX models and full candidate traces."""

import argparse
import hashlib
import json
import platform
import statistics
from datetime import UTC, datetime
from pathlib import Path

from retrieval.contracts import SearchRequest
from retrieval.index import checked_index
from retrieval.metrics import retrieval_metrics
from retrieval.models import LocalModel
from retrieval.service import RetrievalService
from retrieval.vector_store import QdrantVectors
from runtime.artifacts import ArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True)
    parser.add_argument("--index-root", type=Path, default=Path(".local/retrieval/indexes"))
    parser.add_argument(
        "--labels", type=Path, default=Path("evals/retrieval/development-v0.1.json")
    )
    parser.add_argument("--protocol", type=Path, default=Path("evals/retrieval/protocol-v0.1.json"))
    parser.add_argument(
        "--output", type=Path, default=Path(".local/retrieval/development-results.json")
    )
    args = parser.parse_args()
    labels = json.loads(args.labels.read_bytes())
    protocol = json.loads(args.protocol.read_bytes())
    if labels["split"] != "development" or labels["index_artifact_sha256"] != args.index:
        raise ValueError("labels must be development-only and bound to the exact index")
    index = checked_index(args.index_root, args.index)
    expected_config = {
        "candidate_k": protocol["candidate_k_per_channel"],
        "bm25_k1": protocol["bm25"]["k1"],
        "bm25_b": protocol["bm25"]["b"],
        "rrf_constant": protocol["rrf_constant"],
        "rerank_limit": protocol["rerank_limit"],
    }
    if any(index["config"][key] != value for key, value in expected_config.items()):
        raise ValueError("index configuration differs from frozen protocol")
    all_keys = {d["evidence_key"] for d in index["corpus"]["documents"]}
    if any(not set(q["qrels"]) <= all_keys for q in labels["queries"]):
        raise ValueError("labels contain evidence groups outside the fixed corpus")
    if index["encoder"]["synthetic"]:
        raise ValueError("quality comparison requires a real encoder")
    encoder, reranker = LocalModel("encoder"), LocalModel("reranker")
    vector_store = QdrantVectors(index, args.index)
    service = RetrievalService(index, encoder=encoder, reranker=reranker, vector_store=vector_store)
    rows, traces = [], ArtifactStore(Path(".local/retrieval/traces"))
    for method in protocol["methods"]:
        first = SearchRequest.model_validate(labels["queries"][0]["request"] | {"method": method})
        service.search(first)
        for case in labels["queries"]:
            request = SearchRequest.model_validate(case["request"] | {"method": method})
            runs = [service.search(request) for _ in range(3)]
            result = runs[0]
            keys = [h["evidence_key"] for h in result["matched_hits"]]
            assert all([h["evidence_key"] for h in r["matched_hits"]] == keys for r in runs)
            ref = traces.put_json(result)
            grades = case["qrels"]
            assert grades and any(v > 0 for v in grades.values())
            metrics = {str(k): retrieval_metrics(keys, grades, k) for k in protocol["cutoffs"]}
            expected_year = request.scope.model_year
            wrong_years = (
                sum(
                    not any(v["model_year"] == expected_year for v in h["metadata"]["vehicles"])
                    for h in result["matched_hits"]
                )
                if expected_year is not None
                else 0
            )
            exact_hit = (
                any(
                    h["metadata"]["campaign_number"] == case["campaign_number"]
                    for h in result["matched_hits"]
                )
                if case["kind"] == "exact_id"
                else None
            )
            rows.append(
                {
                    "query_id": case["query_id"],
                    "event_cluster_id": case["event_cluster_id"],
                    "method": method,
                    "metrics": metrics,
                    "wrong_year_hits": wrong_years,
                    "exact_identifier_hit_at_10": exact_hit,
                    "pending_hits": len(result["pending_hits"]),
                    "warm_latencies_ms": [r["total_ms"] for r in runs],
                    "median_warm_ms": statistics.median(r["total_ms"] for r in runs),
                    "candidate_trace_artifact": ref,
                    "model_usage": result["model_usage"],
                }
            )
        print(
            json.dumps({"method_completed": method, "queries": len(labels["queries"])}), flush=True
        )
    summary = {}
    for method in protocol["methods"]:
        selected = [r for r in rows if r["method"] == method]
        summary[method] = {
            "metrics": {
                str(k): {
                    metric: statistics.mean(r["metrics"][str(k)][metric] for r in selected)
                    for metric in ("recall", "ndcg")
                }
                for k in protocol["cutoffs"]
            },
            "wrong_year_hits": sum(r["wrong_year_hits"] for r in selected),
            "exact_identifier_hits_at_10": sum(
                r["exact_identifier_hit_at_10"] is True for r in selected
            ),
            "exact_identifier_queries": sum(
                r["exact_identifier_hit_at_10"] is not None for r in selected
            ),
            "median_warm_ms": statistics.median(r["median_warm_ms"] for r in selected),
            "event_macro_ndcg_at_5": statistics.mean(
                statistics.mean(
                    r["metrics"]["5"]["ndcg"] for r in selected if r["event_cluster_id"] == event
                )
                for event in {r["event_cluster_id"] for r in selected}
            ),
        }
    base, reranked = summary["hybrid"], summary["hybrid_rerank"]
    criteria = protocol["default_policy"]["enable_reranker_only_if"]
    improvement = reranked["metrics"]["5"]["ndcg"] - base["metrics"]["5"]["ndcg"]
    recall_ok = reranked["metrics"]["5"]["recall"] >= base["metrics"]["5"]["recall"]
    ratio = reranked["median_warm_ms"] / base["median_warm_ms"]
    use_rerank = (
        improvement >= criteria["mean_ndcg_at_5_improvement_at_least"]
        and recall_ok
        and ratio <= criteria["warm_median_latency_ratio_at_most"]
    )
    report = {
        "schema_version": "retrieval-experiment/0.1",
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_local_onnx_development_experiment_not_heldout_test",
        "index_artifact_sha256": args.index,
        "dataset_build_id": index["dataset_build_id"],
        "labels_sha256": hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "encoder": encoder.identity,
        "reranker": reranker.identity,
        "vector_backend": vector_store.identity,
        "corpus_documents": len(index["corpus"]["documents"]),
        "corpus_evidence_groups": len({d["evidence_key"] for d in index["corpus"]["documents"]}),
        "index_embedding_usage": index["embedding_usage"],
        "queries": len(labels["queries"]),
        "independent_event_clusters": len({q["event_cluster_id"] for q in labels["queries"]}),
        "summary": summary,
        "per_query": rows,
        "default_method": "hybrid_rerank" if use_rerank else "hybrid",
        "default_decision": {
            "ndcg_at_5_delta": improvement,
            "recall_at_5_no_drop": recall_ok,
            "warm_latency_ratio": ratio,
            "rule": "frozen development protocol; no test-set tuning",
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "implementation_sha256": {
            path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                *sorted(Path("retrieval").glob("*.py")),
                Path("scripts/evaluate_retrieval.py"),
            ]
        },
        "limitations": labels["limitations"],
        "external_generative_model_calls": 0,
        "inference_network_requests": 0,
        "dataset_release": None,
    }
    experiment_ref = ArtifactStore(Path(".local/retrieval/experiments")).put_json(report)
    vector_store.close()
    report["experiment_artifact"] = experiment_ref
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": "evaluated", "default_method": report["default_method"], "summary": summary}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

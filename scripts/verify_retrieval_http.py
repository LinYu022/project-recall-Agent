"""Real loopback HTTP and local model/evidence checks against the pinned saved index."""

import argparse
import json
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import uvicorn

from app.retrieval_api import RetrievalSettings, create_retrieval_app
from retrieval.index import checked_index
from runtime.artifacts import ArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path(".local/retrieval/settings.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/retrieval/http-real.json"))
    args = parser.parse_args()
    settings = RetrievalSettings.model_validate_json(args.settings.read_bytes())
    index = checked_index(settings.index_root, settings.index_artifact_sha256)
    app = create_retrieval_app(settings, development_mode=True)
    body = json.loads(Path("examples/retrieval-query.json").read_bytes())
    checks = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=60, trust_env=False
        ) as client:

            def post(name, endpoint, payload, expected=200):
                response = client.post(endpoint, json=payload)
                assert response.status_code == expected, (name, response.status_code)
                value = response.json()
                checks.append(
                    {
                        "case": name,
                        "http_status": response.status_code,
                        "status": value.get("status"),
                        "trace_artifact": value.get("trace_artifact"),
                    }
                )
                return value

            for method in ("bm25", "dense", "hybrid", "hybrid_rerank"):
                result = post(method, "/retrieval/search", body | {"method": method})
                assert result["matched_hits"] and all(
                    any(v["model_year"] == 2022 for v in h["metadata"]["vehicles"])
                    for h in result["matched_hits"]
                )
                saved = ArtifactStore(settings.trace_root).read_json(
                    result["trace_artifact"]["sha256"]
                )
                assert saved["candidate_trace"] == result["candidate_trace"]
            post("wrong_build", "/retrieval/search", body | {"dataset_build_id": "0" * 64}, 422)
            post(
                "outside_sources",
                "/retrieval/search",
                body | {"allowed_source_ids": ["NOT-ALLOWED"]},
                403,
            )
            post("invalid_query", "/retrieval/search", body | {"query": " "}, 422)
            empty = post(
                "known_scope_exclusion", "/retrieval/search", body | {"scope": {"make": "HONDA"}}
            )
            assert not empty["matched_hits"] and empty["model_usage"] == {}
            pending = post(
                "unknown_production_date",
                "/retrieval/search",
                body
                | {
                    "query": "brake",
                    "method": "bm25",
                    "allowed_source_ids": ["NHTSA-COMPLAINTS-F150-2016"],
                    "scope": {
                        "make": "FORD",
                        "model": "F-150",
                        "model_year": 2016,
                        "production_date": "2015-01-01",
                    },
                },
            )
            assert not pending["matched_hits"] and pending["pending_hits"]
            hit = result["matched_hits"][0]
            expanded = post(
                "expand_original",
                "/retrieval/expand",
                {"search": body, "document_id": hit["document_id"]},
            )
            assert expanded["focus"]["bundle"]["focus"]["citations"]
            forbidden = next(
                d
                for d in index["corpus"]["documents"]
                if d["metadata"]["campaign_number"] == "23V838000"
                and d["metadata"]["vehicles"][0]["model_year"] == 2023
            )
            post(
                "excluded_expansion",
                "/retrieval/expand",
                {"search": body, "document_id": forbidden["document_id"]},
                403,
            )
            linked = body | {
                "query": "22V150000 Remedy",
                "method": "bm25",
                "scope": {"make": "FORD", "model": "F-150", "model_year": 2016},
            }
            links = post("official_reference_query", "/retrieval/search", linked)
            target = next(
                h
                for h in links["matched_hits"]
                if h["metadata"]["campaign_number"] == "22V150000"
                and h["metadata"]["official_references"]
            )
            related = post(
                "allowed_related",
                "/retrieval/expand",
                {"search": linked, "document_id": target["document_id"], "include_related": True},
            )
            assert related["related"]
            restricted = linked | {"allowed_source_ids": [target["metadata"]["source_id"]]}
            denied = post(
                "related_source_not_selected",
                "/retrieval/expand",
                {
                    "search": restricted,
                    "document_id": target["document_id"],
                    "include_related": True,
                },
            )
            assert denied["related"] == []
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive(), "retrieval server did not stop"
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_loopback_saved_official_data_and_local_onnx_models",
        "index_artifact_sha256": settings.index_artifact_sha256,
        "dataset_build_id": index["dataset_build_id"],
        "checks": checks,
        "server_stopped": True,
        "new_source_requests": 0,
        "inference_network_requests": 0,
        "external_generative_model_calls": 0,
        "dataset_release": None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "passed", "checks": len(checks), "server_stopped": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

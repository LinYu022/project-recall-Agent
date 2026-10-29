"""Synthetic HTTP contracts share the production filtering and index identity checks."""

from fastapi.testclient import TestClient

from app.retrieval_api import RetrievalSettings, create_retrieval_app
from retrieval.contracts import SearchScope
from retrieval.service import RetrievalService
from tests.test_retrieval import sample as sample


def test_api_search_expand_and_source_build_errors(sample):
    service, request, root = sample
    settings = RetrievalSettings(
        index_artifact_sha256="a" * 64,
        allowed_source_ids=list(service.allowed_sources),
        raw_root=root / "raw",
        parsed_root=root / "parsed",
        chunk_root=root / "chunks",
        trace_root=root / "traces",
    )
    with TestClient(
        create_retrieval_app(settings, service=service, development_mode=True)
    ) as client:
        body = request.model_dump(mode="json")
        response = client.post("/retrieval/search", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["trace_artifact"]["sha256"] and result["pending_hits"]
        hit = result["matched_hits"][0]
        expanded = client.post(
            "/retrieval/expand", json={"search": body, "document_id": hit["document_id"]}
        )
        assert (
            expanded.status_code == 200 and expanded.json()["focus"]["bundle"]["focus"]["citations"]
        )
        assert (
            client.post("/retrieval/search", json=body | {"dataset_build_id": "0" * 64}).status_code
            == 422
        )
        assert (
            client.post(
                "/retrieval/search", json=body | {"allowed_source_ids": ["UNAUTHORIZED"]}
            ).status_code
            == 403
        )
        forbidden = next(
            d["document_id"]
            for d in service.documents
            if d["metadata"]["vehicles"][0]["model_year"] == 2011
        )
        assert (
            client.post(
                "/retrieval/expand", json={"search": body, "document_id": forbidden}
            ).status_code
            == 403
        )
        bad = client.post("/retrieval/search", json=body | {"query": " "})
        assert bad.status_code == 422 and "input" not in str(bad.json())
        empty = client.post(
            "/retrieval/search",
            json=body | {"scope": SearchScope(make="HONDA").model_dump(mode="json")},
        )
        assert empty.status_code == 200 and empty.json()["status"] == "no_matches"


def test_api_missing_model_is_503_without_silent_bm25_fallback(sample):
    service, request, root = sample
    settings = RetrievalSettings(
        index_artifact_sha256="b" * 64,
        allowed_source_ids=list(service.allowed_sources),
        trace_root=root / "traces",
    )
    without = RetrievalService(service.index)
    with TestClient(
        create_retrieval_app(settings, service=without, development_mode=True)
    ) as client:
        response = client.post(
            "/retrieval/search", json=request.model_dump(mode="json") | {"method": "dense"}
        )
        assert response.status_code == 503 and response.json()["status"] == "model_unavailable"

"""Synthetic algorithm and hard-boundary checks; no model quality claims or downloads."""

import math
from copy import deepcopy

import numpy as np
import pytest
from pydantic import ValidationError

from domain.tasks import TimeScope
from entities.scopes import load_scope_rules
from ingestion.records import content_hash
from retrieval.contracts import BuildRequest, RetrievalConfig, SearchRequest, SearchScope
from retrieval.corpus import build_corpus
from retrieval.index import build_index, checked_index
from retrieval.metrics import retrieval_metrics
from retrieval.ranking import bm25_scores, build_bm25, rrf, tokenize
from retrieval.scope import classify_scope
from retrieval.service import ModelsUnavailable, RetrievalService
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.source_fixtures import capture_rows, recall


class FakeEncoder:
    identity = {"synthetic": True, "model": "test-vocabulary", "dimensions": 5}

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        vectors = np.array(
            [
                [text.casefold().count(w) for w in ("brake", "fire", "battery", "update")] + [1]
                for text in texts
            ],
            dtype=np.float32,
        )
        return vectors, {"inputs": len(texts), "truncated_inputs": 0, "synthetic": True}


class FakeReranker:
    identity = {"synthetic": True, "model": "test-remedy-first"}

    def score(self, query, passages):
        return [float("Remedy" in text) for text in passages], {
            "inputs": len(passages),
            "synthetic": True,
        }


@pytest.fixture
def sample(tmp_path):
    from pathlib import Path

    from entities.normalize import EntityRules

    rows = [
        recall()
        | {
            "Make": "TOYOTA",
            "Model": "PRIUS",
            "ModelYear": year,
            "Summary": "SYNTHETIC brake issue",
            "Remedy": "SYNTHETIC brake software update",
        }
        for year in ("2010", "2011", "9999")
    ]
    rows.append(
        recall()
        | {
            "Make": "FORD",
            "Model": "F-150",
            "ModelYear": "2016",
            "Summary": "SYNTHETIC battery fire",
        }
    )
    snapshot = capture_rows(tmp_path / "raw", rows, kind="recall_api")
    build = BuildRequest(
        snapshot_ids=[snapshot.snapshot_id],
        time=TimeScope(
            mode="snapshot_analysis",
            event_interval=None,
            knowledge_cutoff=None,
            date_filter_basis="SYNTHETIC",
        ),
    )
    rules = EntityRules.model_validate_json(Path("examples/retrieval-entities.json").read_bytes())
    corpus = build_corpus(
        tmp_path / "raw",
        tmp_path / "parsed",
        tmp_path / "chunks",
        build,
        rules,
        load_scope_rules(),
        {"documents": []},
    )
    encoder = FakeEncoder()
    index = build_index(corpus, encoder, RetrievalConfig())
    service = RetrievalService(index, encoder=encoder, reranker=FakeReranker())
    request = SearchRequest(
        dataset_build_id=corpus["dataset_build_id"],
        query="brake update",
        allowed_source_ids=[snapshot.source.source_id],
        scope=SearchScope(make="Toyota", model="Prius", model_year=2010),
    )
    return service, request, tmp_path


def test_bm25_tf_saturation_and_exact_identifier_tokens():
    index = build_bm25(["brake brake", "brake fire"])
    scores = bm25_scores(index, "brake", 1.2, 0.75)
    assert math.isclose(scores[0], math.log(1.2) * 2 * 2.2 / 3.2)
    assert scores[0] > scores[1]
    assert tokenize("10V-039 10V039000 10V039123") == ["10v039000", "10v039000", "10v039123"]
    assert bm25_scores(index, "unknown", 1.2, 0.75) == {}


def test_rrf_uses_rank_and_does_not_introduce_new_candidates():
    scores = rrf([[(0, 1000), (1, 1)], [(1, 0.9), (0, 0.8)]], 60)
    assert set(scores) == {0, 1}
    assert scores[0] == scores[1] == 1 / 61 + 1 / 62


@pytest.mark.parametrize(
    "scope,expected",
    [
        ({"model_year": 2010}, "matched"),
        ({"model_year": 2011}, "excluded"),
        ({"production_date": "2009-03-01"}, "pending"),
        ({"configurations": {"engine": "5.0L V8"}}, "pending"),
        ({"component": "UNKNOWN COMPONENT"}, "pending"),
        ({"campaign_number": "10V039000"}, "pending"),
    ],
)
def test_three_valued_missing_metadata_is_never_a_match(scope, expected):
    metadata = {
        "vehicles": [{"make": "TOYOTA", "model": "PRIUS", "model_year": 2010}],
        "components": [None],
        "campaign_number": None,
    }
    assert classify_scope(metadata, SearchScope.model_validate(scope))[0] == expected


def test_known_ranges_and_configurations_exclude_conflicts_and_keep_vehicle_conjunctions():
    metadata = {
        "vehicles": [
            {
                "make": "FORD",
                "model": "F-150",
                "model_year": 2016,
                "production_range": {"start": "2015-01-01", "end": "2015-12-31"},
                "configurations": {"engine": "3.5L ECOBOOST"},
            },
            {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
        ],
        "components": ["POWER TRAIN"],
        "campaign_number": "10V039000",
    }
    assert classify_scope(metadata, SearchScope(make="Ford", model="Prius"))[0] == "excluded"
    assert (
        classify_scope(metadata, SearchScope(make="Ford", production_date="2015-12-31"))[0]
        == "matched"
    )
    assert (
        classify_scope(metadata, SearchScope(make="Ford", production_date="2016-01-01"))[0]
        == "excluded"
    )
    assert (
        classify_scope(metadata, SearchScope(make="Ford", configurations={"engine": "5.0L V8"}))[0]
        == "excluded"
    )
    assert classify_scope(metadata, SearchScope(component="STEERING"))[0] == "excluded"


@pytest.mark.parametrize("method", ["bm25", "dense", "hybrid", "hybrid_rerank"])
def test_all_methods_filter_before_ranking_and_grouping(sample, method):
    service, request, _ = sample
    result = service.search(request.model_copy(update={"method": method}))
    assert result["matched_hits"] and result["pending_hits"]
    assert all(h["metadata"]["vehicles"][0]["model_year"] == 2010 for h in result["matched_hits"])
    assert all(h["metadata"]["vehicles"][0]["model_year"] is None for h in result["pending_hits"])
    assert len({h["evidence_key"] for h in result["matched_hits"]}) == len(result["matched_hits"])
    rejected = {r["document_id"] for r in result["scope_trace"] if r["channel"] == "excluded"}
    assert not any(
        c["document_id"] in rejected
        for stage in result["candidate_trace"]
        for c in stage["candidates"]
    )
    if method == "hybrid_rerank":
        assert result["matched_hits"][0]["field"] == "Remedy"


def test_unscoped_grouping_keeps_references_but_does_not_inflate_top_k(sample):
    service, request, _ = sample
    result = service.search(request.model_copy(update={"scope": SearchScope(), "method": "bm25"}))
    summary = next(h for h in result["matched_hits"] if h["field"] == "Summary")
    assert len(summary["equivalent_document_ids"]) == 3
    assert (
        len([h for h in result["matched_hits"] if h["evidence_key"] == summary["evidence_key"]])
        == 1
    )


def test_no_eligible_candidates_skips_models_and_does_not_fall_back(sample):
    service, request, _ = sample
    before = service.encoder.calls
    result = service.search(
        request.model_copy(update={"scope": SearchScope(make="HONDA"), "method": "hybrid_rerank"})
    )
    assert result["status"] == "no_matches" and service.encoder.calls == before
    assert result["model_usage"] == {}


def test_missing_models_wrong_build_and_permission_expansion_are_explicit(sample):
    service, request, root = sample
    without = RetrievalService(service.index)
    with pytest.raises(ModelsUnavailable):
        without.search(request.model_copy(update={"method": "dense"}))
    with pytest.raises(ModelsUnavailable):
        RetrievalService(service.index, encoder=service.encoder).search(
            request.model_copy(update={"method": "hybrid_rerank"})
        )
    with pytest.raises(ValueError, match="build differs"):
        service.search(request.model_copy(update={"dataset_build_id": "0" * 64}))
    with pytest.raises(PermissionError):
        service.search(request.model_copy(update={"allowed_source_ids": ["UNREGISTERED"]}))
    forbidden = next(
        d["document_id"]
        for d in service.documents
        if d["metadata"]["vehicles"][0]["model_year"] == 2011
    )
    with pytest.raises(PermissionError):
        service.expand(
            request,
            forbidden,
            raw_root=root / "raw",
            parsed_root=root / "parsed",
            chunk_root=root / "chunks",
        )


def test_expansion_replays_original_and_includes_cross_field_limitations(sample):
    service, request, root = sample
    hit = service.search(request)["matched_hits"][0]
    bundle = service.expand(
        request,
        hit["document_id"],
        raw_root=root / "raw",
        parsed_root=root / "parsed",
        chunk_root=root / "chunks",
    )
    assert bundle["focus"]["bundle"]["focus"]["citations"]
    assert any(
        "software update" in n["text"]
        for n in [bundle["focus"]["bundle"]["focus"], *bundle["focus"]["bundle"]["context"]]
    )


@pytest.mark.parametrize("change", ["text", "bm25", "vectors", "build"])
def test_rehashed_but_inconsistent_index_components_are_rejected(sample, change):
    service, _, root = sample
    broken = deepcopy(service.index)
    if change == "text":
        broken["corpus"]["documents"][0]["search_text"] = "SYNTHETIC tampered"
    elif change == "bm25":
        broken["bm25"]["average_length"] += 1
    elif change == "vectors":
        broken["vectors"][0] = [0] * 5
    else:
        broken["dataset_build_id"] = "0" * 64
    ref = ArtifactStore(root / "index").put_json(broken)
    with pytest.raises(ArtifactIntegrityError):
        checked_index(root / "index", ref["sha256"])


def test_index_roundtrip_and_query_encoder_identity(sample):
    service, _, root = sample
    ref = ArtifactStore(root / "index").put_json(service.index)
    assert checked_index(root / "index", ref["sha256"]) == service.index
    fake = FakeEncoder()
    fake.identity = {"synthetic": True, "model": "changed"}
    with pytest.raises(ValueError, match="encoder differs"):
        RetrievalService(service.index, encoder=fake)


def test_metrics_are_graded_unique_and_do_not_invent_zero_for_unlabelled_queries():
    scores = retrieval_metrics(["b", "a", "irrelevant"], {"a": 3, "b": 1}, 1)
    assert scores == {"recall": 0.5, "ndcg": 1 / 7, "reason": None}
    assert retrieval_metrics([], {"a": 3}, 5)["recall"] == 0
    assert retrieval_metrics([], {}, 5)["ndcg"] is None
    with pytest.raises(ValueError, match="duplicate"):
        retrieval_metrics(["a", "a"], {"a": 3}, 5)


def test_request_rejects_empty_query_duplicate_sources_and_invalid_limits(sample):
    _, request, _ = sample
    for patch in [{"query": " "}, {"allowed_source_ids": ["ONE", "ONE"]}, {"top_k": 0}]:
        with pytest.raises(ValidationError):
            SearchRequest.model_validate(request.model_dump() | patch)
    assert content_hash(request.model_dump(mode="json"))

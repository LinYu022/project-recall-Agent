"""Synthetic replay and failure diagnosis; imported receipts never become new old observations."""

import json

import pytest

from ingestion.snapshots import FetchResult, SnapshotStore
from retrieval.diagnostics import diagnose
from scripts.restore_retrieval_sources import restore_sources
from tests.source_fixtures import capture_rows, recall
from tests.test_retrieval import sample as sample


def test_restoration_requires_exact_bytes_and_keeps_actual_new_fetch_separate(tmp_path):
    original = capture_rows(tmp_path / "original", [recall()], kind="recall_api")
    body = SnapshotStore(tmp_path / "original").raw_bytes(original.raw)
    root = tmp_path / "restored"
    calls = []

    def fetch(spec):
        calls.append(spec.source_id)
        return FetchResult(200, {}, body)

    report = restore_sources(root, [original.model_dump(mode="json")], fetch)
    assert report["restored_recorded_receipts"] == [original.snapshot_id]
    assert len(report["new_actual_fetch_observations"]) == 1
    assert SnapshotStore(root).verify(original.snapshot_id) == original
    new = SnapshotStore(root).verify(report["new_actual_fetch_observations"][0])
    assert new.snapshot_id != original.snapshot_id and new.fetched_at >= original.fetched_at
    assert restore_sources(root, [original.model_dump(mode="json")], fetch)[
        "already_verified_receipts"
    ] == [original.snapshot_id]
    assert len(calls) == 1


def test_changed_current_bytes_cannot_be_relabelled_as_old_receipt(tmp_path):
    original = capture_rows(tmp_path / "original", [recall()], kind="recall_api")
    changed = json.dumps(
        {"Count": 1, "results": [recall() | {"Summary": "SYNTHETIC changed"}]}
    ).encode()
    root = tmp_path / "new"
    with pytest.raises(ValueError, match="current bytes differ"):
        restore_sources(
            root, [original.model_dump(mode="json")], lambda _: FetchResult(200, {}, changed)
        )
    assert not (root / "snapshots" / (original.snapshot_id + ".json")).exists()
    assert len(list((root / "snapshots").glob("*.json"))) == 1


def test_diagnosis_distinguishes_index_scope_candidate_and_rank_loss(sample):
    service, request, _ = sample
    target = next(d["evidence_key"] for d in service.documents if d["field"] == "Remedy")
    result = service.search(
        request.model_copy(update={"method": "bm25", "query": "unseenzzztoken"})
    )
    assert diagnose(service.index, result, {target}, 1)["diagnosis"] == "not_retrieved"
    request = request.model_copy(update={"method": "hybrid_rerank"})
    result = service.search(request)
    assert diagnose(service.index, result, {target}, 1)["diagnosis"] == "retrieved"
    result["matched_hits"] = []
    assert diagnose(service.index, result, {target}, 1)["diagnosis"] == "ranked_below_cutoff"
    from retrieval.contracts import SearchScope

    result = service.search(request.model_copy(update={"scope": SearchScope(make="HONDA")}))
    assert diagnose(service.index, result, {target}, 1)["diagnosis"] == "filtered_or_not_allowed"
    assert diagnose(service.index, result, {"0" * 64}, 1)["diagnosis"] == "not_indexed"

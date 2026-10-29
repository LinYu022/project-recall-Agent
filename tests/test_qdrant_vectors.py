"""Real persistent Qdrant local tests with synthetic vectors, no downloaded weights."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
import pytest
from qdrant_client import models

from retrieval.service import RetrievalService
from retrieval.vector_store import NumpyVectors, QdrantVectors
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.test_retrieval import sample as sample


def test_persistent_qdrant_matches_reference_and_applies_id_filter(sample):
    service, request, root = sample
    digest = ArtifactStore(root / "index").put_json(service.index)["sha256"]
    q = QdrantVectors(service.index, digest, root / "qdrant", create=True)
    n = NumpyVectors(service.index)
    vector = np.array([1, 0, 0, 1, 1], dtype=np.float32)
    vector /= np.linalg.norm(vector)
    allowed = {0, 2}
    actual = q.scores(vector, allowed)
    assert set(actual) == allowed and actual == pytest.approx(n.scores(vector, allowed), abs=1e-6)
    assert q.scores(vector, set()) == {}
    integrated = RetrievalService(
        service.index, encoder=service.encoder, reranker=service.reranker, vector_store=q
    )
    result = integrated.search(request)
    assert result["vector_backend"]["mode"] == "persistent_local_exact"
    assert all(h["metadata"]["vehicles"][0]["model_year"] == 2010 for h in result["matched_hits"])
    q.close()
    reopened = QdrantVectors(service.index, digest, root / "qdrant")
    assert reopened.scores(vector, allowed) == pytest.approx(actual, abs=1e-6)
    reopened.close()


@pytest.mark.parametrize("change", ["delete", "payload", "vector"])
def test_qdrant_payload_vector_and_point_count_mismatch_block_loading(sample, change):
    service, _, root = sample
    digest = ArtifactStore(root / "index").put_json(service.index)["sha256"]
    q = QdrantVectors(service.index, digest, root / "qdrant", create=True)
    point = q.point_ids[0]
    if change == "delete":
        q.client.delete("documents", points_selector=models.PointIdsList(points=[point]), wait=True)
    elif change == "payload":
        q.client.set_payload(
            "documents", payload={"document_id": "SYNTHETIC-WRONG"}, points=[point], wait=True
        )
    else:
        q.client.update_vectors(
            "documents",
            points=[models.PointVectors(id=point, vector=[1.0, 0.0, 0.0, 0.0, 0.0])],
            wait=True,
        )
    q.close()
    with pytest.raises(ArtifactIntegrityError):
        QdrantVectors(service.index, digest, root / "qdrant")


def test_missing_qdrant_component_never_creates_or_falls_back(sample):
    service, _, root = sample
    with pytest.raises(FileNotFoundError):
        QdrantVectors(service.index, "a" * 64, root / "qdrant")
    with pytest.raises(ValueError):
        QdrantVectors(service.index, "../outside", root / "qdrant", create=True)


def test_same_directory_clients_wait_until_owner_closes(sample):
    service, _, root = sample
    digest = ArtifactStore(root / "index").put_json(service.index)["sha256"]
    owner = QdrantVectors(service.index, digest, root / "qdrant", create=True)
    attempted, opened = Event(), Event()

    def read_after_owner():
        attempted.set()
        reader = QdrantVectors(service.index, digest, root / "qdrant")
        try:
            opened.set()
            return reader.client.get_collection("documents").points_count
        finally:
            reader.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read_after_owner)
        try:
            assert attempted.wait(5)
            assert not opened.wait(0.1)
        finally:
            owner.close()
            owner.close()  # Idempotent: never release another owner's reservation.
        assert future.result(timeout=5) == len(service.index["corpus"]["documents"])
        assert opened.is_set()


@pytest.mark.parametrize("failure", ["manifest_missing", "client_constructor", "integrity"])
def test_failed_constructor_releases_directory_for_other_thread(sample, monkeypatch, failure):
    import retrieval.vector_store as module

    service, _, root = sample
    digest = ArtifactStore(root / "index").put_json(service.index)["sha256"]
    real_client = module.QdrantClient
    if failure == "manifest_missing":
        with pytest.raises(FileNotFoundError):
            QdrantVectors(service.index, digest, root / "qdrant")
    else:
        owner = QdrantVectors(service.index, digest, root / "qdrant", create=True)
        owner.close()
        if failure == "client_constructor":

            def broken_client(**kwargs):
                raise RuntimeError("SIMULATED constructor failure")

            monkeypatch.setattr(module, "QdrantClient", broken_client)
            with pytest.raises(RuntimeError):
                QdrantVectors(service.index, digest, root / "qdrant")
            monkeypatch.setattr(module, "QdrantClient", real_client)
        else:
            broken_index = service.index | {"vectors": [[0] * 5] * len(owner.point_ids)}
            with pytest.raises(ArtifactIntegrityError):
                QdrantVectors(broken_index, digest, root / "qdrant")

    def reopen():
        reader = QdrantVectors(service.index, digest, root / "qdrant", create=True)
        reader.close()
        return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(reopen).result(timeout=5)


def test_cli_build_returns_pinned_server_config_and_reuses_identical_components(
    sample, monkeypatch, capsys
):
    import json
    from pathlib import Path

    from retrieval.__main__ import main
    from tests.test_retrieval import FakeEncoder

    service, _, root = sample
    request = root / "build.json"
    request.write_text(json.dumps(service.index["corpus"]["recipe"]["request"]), encoding="utf-8")
    monkeypatch.setattr("retrieval.__main__.LocalModel", lambda *args: FakeEncoder())
    args = [
        "build",
        str(request),
        "--raw-root",
        str(root / "raw"),
        "--parsed-root",
        str(root / "parsed"),
        "--chunk-root",
        str(root / "chunks"),
        "--index-root",
        str(root / "cli-index"),
        "--qdrant-root",
        str(root / "cli-qdrant"),
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    settings = json.loads(Path(first["server_config_path"]).read_bytes())
    assert settings["index_artifact_sha256"] == first["index_artifact"]["sha256"]
    assert settings["qdrant_root"] == (root / "cli-qdrant").as_posix()
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["index_artifact"] == first["index_artifact"]

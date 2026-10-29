"""Pinned Qdrant local collections and an exact NumPy reference, sharing explicit ID filters."""

import importlib.metadata
import json
import os
import re
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from qdrant_client import QdrantClient, models

from runtime.artifacts import ArtifactIntegrityError

# Qdrant local permits one client per directory, including integrity-check clients.
# Keep the lock for the complete client lifetime, not just construction/query calls.
_DIRECTORY_LOCKS: dict[str, RLock] = {}
_REGISTRY_LOCK = RLock()


def _directory_lock(directory: Path) -> RLock:
    key = os.path.normcase(str(directory.resolve()))
    with _REGISTRY_LOCK:
        return _DIRECTORY_LOCKS.setdefault(key, RLock())


class NumpyVectors:
    def __init__(self, index: dict):
        self.vectors = np.asarray(index["vectors"], dtype=np.float32)
        self.identity = {"backend": "numpy_exact_reference", "numpy": np.__version__}

    def scores(self, vector: np.ndarray, eligible: set[int]) -> dict[int, float]:
        return {i: float(self.vectors[i] @ vector) for i in sorted(eligible)}


class QdrantVectors:
    def __init__(
        self,
        index: dict,
        digest: str,
        root=Path(".local/retrieval/qdrant"),
        *,
        create=False,
        compact=False,
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid pinned index digest")
        self.directory = root / (digest[:24] if compact else digest)
        self._storage_lock = _directory_lock(self.directory)
        self._closed = False
        self._storage_lock.acquire()
        try:
            self._open(index, digest, create)
        except BaseException:
            self.close()
            raise

    def _open(self, index: dict, digest: str, create: bool):
        self.identifiers = [d["document_id"] for d in index["corpus"]["documents"]]
        self.point_ids = [str(uuid5(NAMESPACE_URL, identifier)) for identifier in self.identifiers]
        if len(self.point_ids) != len(set(self.point_ids)):
            raise ValueError("Qdrant point ID collision")
        manifest = {
            "schema_version": "qdrant-component/0.1",
            "index_artifact_sha256": digest,
            "dataset_build_id": index["dataset_build_id"],
            "dimensions": index["vector_dimensions"],
            "documents": self.identifiers,
            "qdrant_client": importlib.metadata.version("qdrant-client"),
            "distance": "Cosine",
            "collection": "documents",
            "mode": "persistent_local_exact",
        }
        path = self.directory / "component.json"
        existing = path.exists()
        if not existing and not create:
            raise FileNotFoundError("pinned Qdrant component missing; no reference fallback")
        if existing and json.loads(path.read_bytes()) != manifest:
            raise ArtifactIntegrityError("Qdrant component manifest differs from index/runtime")
        if existing and not (self.directory / "database/meta.json").is_file():
            raise FileNotFoundError("Qdrant database missing; no automatic reconstruction")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = QdrantClient(
            path=str(self.directory / "database"), force_disable_check_same_thread=True
        )
        if not existing:
            if self.client.collection_exists("documents"):
                raise ArtifactIntegrityError(
                    "uncommitted Qdrant collection requires explicit rebuild"
                )
            self.client.create_collection(
                "documents",
                vectors_config=models.VectorParams(
                    size=index["vector_dimensions"], distance=models.Distance.COSINE
                ),
            )
            for start in range(0, len(self.point_ids), 128):
                self.client.upsert(
                    "documents",
                    points=[
                        models.PointStruct(
                            id=self.point_ids[i],
                            vector=index["vectors"][i],
                            payload={
                                "position": i,
                                "document_id": self.identifiers[i],
                                "dataset_build_id": index["dataset_build_id"],
                            },
                        )
                        for i in range(start, min(start + 128, len(self.point_ids)))
                    ],
                    wait=True,
                )
        collection = self.client.get_collection("documents")
        if collection.points_count != len(self.identifiers):
            raise ArtifactIntegrityError("Qdrant point count differs")
        records = self.client.retrieve(
            "documents", ids=self.point_ids, with_payload=True, with_vectors=True
        )
        by_id = {str(record.id): record for record in records}
        for i, identifier in enumerate(self.point_ids):
            actual = by_id[identifier]
            if not isinstance(actual.vector, list):
                raise ArtifactIntegrityError("Qdrant dense vector missing")
            if actual.payload != {
                "position": i,
                "document_id": self.identifiers[i],
                "dataset_build_id": index["dataset_build_id"],
            } or not np.allclose(actual.vector, index["vectors"][i], atol=1e-6, rtol=1e-6):
                raise ArtifactIntegrityError("Qdrant payload/vector differs from pinned index")
        if not existing:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        self.identity = {k: v for k, v in manifest.items() if k != "documents"}

    def scores(self, vector: np.ndarray, eligible: set[int]) -> dict[int, float]:
        if not eligible:
            return {}
        result = self.client.query_points(
            "documents",
            query=vector.tolist(),
            query_filter=models.Filter(
                must=[models.HasIdCondition(has_id=[self.point_ids[i] for i in sorted(eligible)])]
            ),
            limit=len(eligible),
            with_payload=True,
        ).points
        scores = {}
        for point in result:
            if point.payload is None or type(point.payload.get("position")) is not int:
                raise ArtifactIntegrityError("Qdrant position payload missing")
            position = point.payload["position"]
            if (
                position not in eligible
                or point.payload.get("document_id") != self.identifiers[position]
            ):
                raise ArtifactIntegrityError("Qdrant returned a foreign document")
            scores[position] = float(point.score)
        if set(scores) != eligible:
            raise ArtifactIntegrityError("Qdrant query returned outside/missing filtered points")
        return scores

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if hasattr(self, "client"):
                self.client.close()
        finally:
            self._storage_lock.release()

import math
from pathlib import Path

import numpy as np

from ingestion.records import content_hash
from parsing.parser import text_hash
from retrieval.contracts import RetrievalConfig
from retrieval.models import Encoder
from retrieval.ranking import build_bm25
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def build_index(corpus: dict, encoder: Encoder, config: RetrievalConfig) -> dict:
    texts = [d["search_text"] for d in corpus["documents"]]
    if not texts:
        raise ValueError("cannot build a retrieval index over no documents")
    vectors, usage = encoder.encode(texts)
    if vectors.ndim != 2 or len(vectors) != len(texts) or not np.isfinite(vectors).all():
        raise ValueError("encoder returned invalid vectors")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("encoder returned zero vectors")
    return {
        "schema_version": "retrieval-index/0.1",
        "engine_version": "local-retrieval/0.1",
        "dataset_build_id": corpus["dataset_build_id"],
        "corpus": corpus,
        "config": config.model_dump(mode="json"),
        "encoder": encoder.identity,
        "embedding_usage": usage,
        "vector_dimensions": int(vectors.shape[1]),
        "vectors": (vectors / norms).tolist(),
        "bm25": build_bm25(texts),
        "dataset_release": None,
    }


def checked_index(root: Path, digest: str) -> dict:
    index = ArtifactStore(root).read_json(digest)
    if (
        index.get("schema_version") != "retrieval-index/0.1"
        or index.get("engine_version") != "local-retrieval/0.1"
    ):
        raise ValueError("unsupported retrieval index")
    RetrievalConfig.model_validate(index["config"])
    corpus = index["corpus"]
    docs = corpus["documents"]
    identifiers = []
    for doc in docs:
        body = {k: v for k, v in doc.items() if k != "document_id"}
        if (
            content_hash(body) != doc["document_id"]
            or text_hash(doc["search_text"]) != doc["search_text_sha256"]
        ):
            raise ArtifactIntegrityError("indexed document identity differs")
        identifiers.append(doc["document_id"])
    build_id = content_hash({"recipe": corpus["recipe"], "document_ids": identifiers})
    if (
        len(identifiers) != len(set(identifiers))
        or build_id != index["dataset_build_id"]
        or build_id != corpus["dataset_build_id"]
    ):
        raise ArtifactIntegrityError("corpus/build identity differs")
    vectors = np.asarray(index["vectors"])
    if (
        vectors.ndim != 2
        or len(vectors) != len(docs)
        or vectors.shape[1] != index["vector_dimensions"]
        or not np.isfinite(vectors).all()
    ):
        raise ArtifactIntegrityError("index vectors invalid")
    if any(not math.isclose(float(v), 1, abs_tol=1e-5) for v in np.linalg.norm(vectors, axis=1)):
        raise ArtifactIntegrityError("index vectors not normalized")
    if index["bm25"] != build_bm25([d["search_text"] for d in docs]):
        raise ArtifactIntegrityError("BM25 component differs from corpus")
    return index

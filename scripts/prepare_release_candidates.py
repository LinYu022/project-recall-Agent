"""Prepare two real, local-source release candidates; no official network requests."""

import json
from pathlib import Path

from entities.normalize import EntityRules
from entities.scopes import load_scope_rules
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from releases.bundle import BundleStore, read_json
from retrieval.contracts import BuildRequest, RetrievalConfig
from retrieval.corpus import build_corpus
from retrieval.index import build_index, checked_index
from retrieval.models import LocalModel
from runtime.artifacts import ArtifactStore


def prepare(root=Path(".local/releases")):
    encoder, reranker = LocalModel("encoder"), LocalModel("reranker")
    request = read_json(Path("examples/retrieval-build.json"))
    request["snapshot_ids"] = [
        "61cd818ad3ae45c7bbdc94ecf0b479bc",
        "d0be027566e94ad2aa0737d166df64b1",
        "9d2d065bf1f844e7ab4ea63494a4b2f9",
    ]
    rules = {
        "entity": read_json(Path("examples/retrieval-entities.json")),
        "scope": load_scope_rules().model_dump(mode="json"),
        "pdf": read_json(Path("examples/retrieval-pdf-scope.json")),
        "chunk": load_chunk_config("structure").model_dump(mode="json"),
        "structure": load_structure_rules().model_dump(mode="json"),
    }
    corpus = build_corpus(
        Path(".local/source-snapshots"),
        Path(".local/parsed-sources"),
        Path(".local/chunk-sets"),
        BuildRequest.model_validate(request),
        EntityRules.model_validate(rules["entity"]),
        load_scope_rules(),
        rules["pdf"],
    )
    index = build_index(corpus, encoder, RetrievalConfig())
    baseline = ArtifactStore(Path(".local/retrieval/indexes")).put_json(index)
    digests = {
        "baseline": baseline["sha256"],
        "expanded": "46373e84c19beb44a37ed3b2a2cf079d98b86ebf15f7a6cfbcc2b8852a616982",
    }
    store = BundleStore(root)
    candidates = {}
    for name, digest in digests.items():
        source_index = checked_index(Path(".local/retrieval/indexes"), digest)
        candidates[name] = store.create(
            Path(".local/retrieval/indexes"),
            digest,
            raw_root=Path(".local/source-snapshots"),
            parsed_root=Path(".local/parsed-sources"),
            chunk_root=Path(".local/chunk-sets"),
            model_root=Path(".local/retrieval/models"),
            encoder_identity=encoder.identity,
            reranker_identity=reranker.identity,
            rules=rules,
        )
        candidates[name]["index_artifact_sha256"] = digest
        candidates[name]["documents"] = len(source_index["corpus"]["documents"])
        print(json.dumps({"sealed": name, **candidates[name]}), flush=True)
    result = {
        "classification": "saved_official_source_bundles_and_real_onnx_models",
        "root": root.as_posix(),
        "candidates": candidates,
        "new_source_requests": 0,
        "local_embedding_inputs": len(corpus["documents"]),
    }
    Path(".local/release-candidates.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    prepare()

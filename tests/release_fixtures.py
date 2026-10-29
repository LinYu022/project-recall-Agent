"""Small synthetic source releases; no downloaded model files or inference required."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from domain.tasks import TimeScope
from entities.normalize import EntityRules
from entities.scopes import load_scope_rules
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from releases.bundle import BundleStore
from retrieval.contracts import BuildRequest, RetrievalConfig
from retrieval.corpus import build_corpus
from retrieval.index import build_index
from runtime.artifacts import ArtifactStore
from tests.source_fixtures import capture_rows, recall
from tests.test_retrieval import FakeEncoder, FakeReranker


@pytest.fixture
def release_factory(tmp_path):
    root = tmp_path.parent / ("rel-" + uuid4().hex[:8])
    store = BundleStore(root / "releases", allow_synthetic=True)
    entity = EntityRules.model_validate_json(Path("examples/retrieval-entities.json").read_bytes())
    scope = load_scope_rules()
    rules = {
        "entity": entity.model_dump(mode="json"),
        "scope": scope.model_dump(mode="json"),
        "pdf": {"documents": []},
        "structure": load_structure_rules().model_dump(mode="json"),
        "chunk": load_chunk_config("structure").model_dump(mode="json"),
    }

    def create(label, *, campaign="10V039000", also_campaigns=()):
        snapshot = capture_rows(
            root / "raw",
            [
                recall()
                | {
                    "Make": "TOYOTA",
                    "Model": "PRIUS",
                    "ModelYear": "2010",
                    "NHTSACampaignNumber": campaign,
                    "Summary": f"SYNTHETIC release {label} brake condition",
                    "Remedy": f"SYNTHETIC {label} repair",
                }
            ],
            kind="recall_api",
            campaign=campaign if campaign != "10V039000" else None,
        )
        request = BuildRequest(
            snapshot_ids=[snapshot.snapshot_id]
            + [
                capture_rows(
                    root / "raw",
                    [
                        recall()
                        | {
                            "NHTSACampaignNumber": code,
                            "Make": "TOYOTA",
                            "Model": "PRIUS",
                            "Summary": f"SYNTHETIC {label} independent scope {code}",
                        }
                    ],
                    kind="recall_api",
                    campaign=code,
                ).snapshot_id
                for code in also_campaigns
            ],
            time=TimeScope(
                mode="snapshot_analysis",
                event_interval=None,
                knowledge_cutoff=None,
                date_filter_basis="SYNTHETIC frozen receipts",
            ),
        )
        corpus = build_corpus(
            root / "raw", root / "parsed", root / "chunks", request, entity, scope, rules["pdf"]
        )
        index = build_index(corpus, FakeEncoder(), RetrievalConfig())
        ref = ArtifactStore(root / "indexes").put_json(index)
        sealed = store.create(
            root / "indexes",
            ref["sha256"],
            raw_root=root / "raw",
            parsed_root=root / "parsed",
            chunk_root=root / "chunks",
            model_root=root / "models",
            encoder_identity=FakeEncoder.identity,
            reranker_identity=FakeReranker.identity,
            rules=rules,
            retention_days=1,
        )
        manifest = store.manifests.read_json(sealed["manifest_sha256"])
        return {
            "store": store,
            "sealed": sealed,
            "manifest": manifest,
            "directory": store.directory(manifest),
            "snapshot": snapshot,
            "index": index,
        }

    return create


def write_changed_manifest(manifest):
    from ingestion.records import content_hash

    return {
        **manifest,
        "release_id": content_hash({k: v for k, v in manifest.items() if k != "release_id"}),
    }


def alter_component(bundle, path, value):
    from copy import deepcopy

    from releases.bundle import file_sha

    target = bundle["directory"] / path
    target.write_text(json.dumps(value), encoding="utf-8")
    manifest = deepcopy(bundle["manifest"])
    for entry in manifest["inventory"]:
        if entry["path"] == path:
            entry.update(sha256=file_sha(target), bytes=target.stat().st_size)
    return write_changed_manifest(manifest)

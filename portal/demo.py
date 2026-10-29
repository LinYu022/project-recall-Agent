"""Offline, explicitly synthetic fixtures; never imports tests or a provider adapter."""

import json
from pathlib import Path

import numpy as np

from domain.tasks import TimeScope
from entities.normalize import EntityRules
from entities.scopes import load_scope_rules
from ingestion.contracts import SourceSpec
from ingestion.snapshots import FetchResult, SnapshotStore
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from releases.bundle import BundleStore
from retrieval.contracts import BuildRequest, RetrievalConfig
from retrieval.corpus import build_corpus
from retrieval.index import build_index
from runtime.artifacts import ArtifactStore


class DemoEncoder:
    identity = {"synthetic": True, "model": "product-demo-vocabulary/1", "dimensions": 5}

    def encode(self, texts):
        vectors = np.array(
            [
                [t.lower().count(w) for w in ("brake", "door", "risk", "repair")] + [1]
                for t in texts
            ],
            dtype=np.float32,
        )
        return vectors, {"inputs": len(texts), "truncated_inputs": 0, "synthetic": True}


class DemoReranker:
    identity = {"synthetic": True, "model": "product-demo-remedy/1"}

    def score(self, query, passages):
        return [float("Remedy" in t) for t in passages], {"synthetic": True}


class DemoModel:
    identity = {"model_id": "product-demo-abstention/1", "is_simulated": True}

    def generate(self, system, payload, *, max_tokens):
        if "[DEMO_FAIL]" in payload.get("question", ""):
            from models.chat_api import ModelCallFailed

            raise ModelCallFailed("synthetic_transport_failure_no_external_request")
        return {
            "proposal": {
                "disposition": "insufficient_evidence",
                "claims": [],
                "unresolved_fields": ["source_coverage"],
            },
            "accepted_complete": True,
            "usage": None,
            "estimated_cost": None,
            "elapsed_ms": 0,
        }


def synthetic_bundle(root):
    store = BundleStore(root / "releases", allow_synthetic=True)
    snapshots = []
    for code, year, summary in [
        ("10V039000", "2010", "SYNTHETIC: certain Prius vehicles, brake issue on rough roads."),
        ("17V331000", "2016", "SYNTHETIC: certain F-150 vehicles with accessory door covers."),
        ("17V652000", "2016", "SYNTHETIC: certain F-150 door cables bend or freeze."),
    ]:
        ford = year == "2016"
        source = SourceSpec(
            source_id="DEMO-CAMPAIGN-" + code,
            kind="recall_api",
            url="https://example.invalid/synthetic/" + code,
            selection={"campaign_number": code},
        )
        body = json.dumps(
            {
                "Count": 1,
                "results": [
                    {
                        "NHTSACampaignNumber": code,
                        "Make": "FORD" if ford else "TOYOTA",
                        "Model": "F-150" if ford else "PRIUS",
                        "ModelYear": year,
                        "Component": "LATCHES/LOCKS/LINKAGES" if ford else "SERVICE BRAKES",
                        "Summary": summary,
                        "Consequence": "SYNTHETIC: may increase crash risk; not proof of causation.",
                        "Remedy": "SYNTHETIC: dealer repair; not a permanent safety guarantee.",
                        "ReportReceivedDate": "01/01/2020",
                    }
                ],
            }
        ).encode()

        def fetch(_):
            return FetchResult(200, {}, body)

        snap = SnapshotStore(root / "raw").capture(source, fetch)
        snapshots.append(snap.snapshot_id)
    rules = {
        "entity": json.loads(Path("examples/retrieval-entities.json").read_bytes()),
        "scope": load_scope_rules().model_dump(mode="json"),
        "pdf": {"documents": []},
        "structure": load_structure_rules().model_dump(mode="json"),
        "chunk": load_chunk_config("structure").model_dump(mode="json"),
    }
    corpus = build_corpus(
        root / "raw",
        root / "parsed",
        root / "chunks",
        BuildRequest(
            snapshot_ids=snapshots,
            time=TimeScope(
                mode="snapshot_analysis",
                event_interval=None,
                knowledge_cutoff=None,
                date_filter_basis="SYNTHETIC local product demo; no historical reconstruction",
            ),
        ),
        EntityRules.model_validate(rules["entity"]),
        load_scope_rules(),
        rules["pdf"],
    )
    index = build_index(corpus, DemoEncoder(), RetrievalConfig())
    ref = ArtifactStore(root / "index").put_json(index)
    sealed = store.create(
        root / "index",
        ref["sha256"],
        raw_root=root / "raw",
        parsed_root=root / "parsed",
        chunk_root=root / "chunks",
        model_root=root / "models",
        encoder_identity=DemoEncoder.identity,
        reranker_identity=DemoReranker.identity,
        rules=rules,
    )
    return store, sealed

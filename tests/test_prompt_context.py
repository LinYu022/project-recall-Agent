"""Synthetic retention and corruption checks, not model quality measurements."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from ingestion.records import content_hash
from investigation.agent_contracts import Decision
from investigation.contracts import EvidenceCard
from investigation.flow import prompt_cards
from investigation.prompt_context import (
    ContextArchive,
    ContextBinding,
    build_context,
    evidence_page,
    json_text,
    restore_summary,
    summarize,
    text_page,
)
from models.chat_api import ModelCallFailed
from parsing.parser import text_hash
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.test_single_agent import decision

H = "a" * 64


def fixture_data():
    binding = ContextBinding(
        task_id="CONTEXT-TEST",
        release_id=H,
        task_spec_sha256=H,
        plan_sha256=H,
        revision_id="REV-CURRENT",
        authorization_id="GRANT",
        epoch=2,
    )
    values = []
    for i, (text, dependencies) in enumerate(
        [
            (
                "SYNTHETIC subject vehicles: Model Year 2010 Toyota Prius; individual applicability unknown.",
                [],
            ),
            (
                "SYNTHETIC closure does NOT establish universal safety; only under ABS conditions.",
                [0],
            ),
            *[
                (f"SYNTHETIC unrelated observation {i} " + "ordinary text " * 400, [])
                for i in range(6)
            ],
        ]
    ):
        payload = {
            "text": text,
            "citations": [
                {
                    "source_version_id": H,
                    "unit_id": H,
                    "start": 0,
                    "end": len(text),
                    "quote_sha256": text_hash(text),
                }
            ],
        }
        body = {
            "schema_version": "evidence-card/0.1",
            "evidence_key": content_hash([text]),
            "task_id": binding.task_id,
            "release_id": H,
            "kind": "text",
            "origin_sha256": content_hash([i]),
            "selector": {"path": ["output"]},
            "source_ids": ["SYNTHETIC-SOURCE"],
            "source_version_ids": [H],
            "scope_channel": "matched",
            "payload": payload,
            "required_context_ids": [values[x].evidence_id for x in dependencies],
        }
        values.append(EvidenceCard.model_validate(body | {"evidence_id": content_hash(body)}))
    aliases, visible = prompt_cards(values)
    history = [
        {
            "revision": i + 1,
            "reason": "SYNTHETIC long trace " + "filler " * 200,
            "hypotheses": [
                {
                    "hypothesis_id": "H001",
                    "statement": "SYNTHETIC broad claim contradicted",
                    "status": "contradicted",
                    "support_ids": [],
                    "counter_ids": ["E002"],
                }
            ]
            if i == 0
            else [],
            "evidence_gaps": ["individual_vehicle"] if i == 0 else [],
            "next_steps": [],
        }
        for i in range(20)
    ]
    payload = {
        "question": "SYNTHETIC Prius safety?",
        "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
        "time": {"mode": "snapshot_analysis"},
        "initial_plan": {"goal": "SYNTHETIC bounded investigation"},
        "evidence": visible,
        "candidates": [],
        "observations": [{"index": i} for i in range(20)],
        "previous_decisions": history,
        "available_tools": ["read", "finish"],
        "authorization_limits": {"allowed_source_ids": ["SYNTHETIC-SOURCE"]},
    }
    return binding, values, payload, aliases


def test_early_counter_scope_negation_and_unresolved_survive_only_structured_budget(tmp_path):
    binding, cards, payload, _ = fixture_data()
    results = {}
    for mode in ("full_history", "recent_window", "structured"):
        results[mode] = build_context(
            "json",
            payload,
            cards,
            binding,
            {"E002"},
            ContextArchive(ArtifactStore(tmp_path / mode)),
            mode=mode,
            cap=12000,
        )
    full, recent, structured = (results[x] for x in ("full_history", "recent_window", "structured"))
    assert full[1]["after_characters"] > 12000
    assert "does NOT establish" not in json_text(recent[0])
    assert structured[1]["after_characters"] <= 12000
    assert "does NOT establish" in json_text(structured[0])
    assert "Model Year 2010" in json_text(structured[0])
    assert structured[0]["context_summary"]["current_hypotheses"][0]["status"] == "contradicted"
    assert structured[0]["context_summary"]["unresolved_conditions"] == ["individual_vehicle"]
    assert structured[0]["context_summary"]["omitted_evidence_groups"] > 0


@pytest.mark.parametrize(
    "field,value",
    [("revision_id", "OLD"), ("authorization_id", "OLD"), ("epoch", 3), ("release_id", "b" * 64)],
)
def test_recovery_refuses_stale_revision_grant_epoch_or_release(tmp_path, field, value):
    binding, cards, payload, _ = fixture_data()
    archive = ContextArchive(ArtifactStore(tmp_path))
    _, record = build_context(
        "json", payload, cards, binding, {"E002"}, archive, mode="structured", cap=12000
    )
    with pytest.raises(ArtifactIntegrityError, match="changed"):
        restore_summary(
            archive.store,
            record["summary_artifact"],
            binding.model_copy(update={field: value}),
            expected_snapshot=record["snapshot_artifact"]["sha256"],
        )


@pytest.mark.parametrize("change", ["negation", "version", "missing_snapshot"])
def test_self_consistent_tampered_summary_still_fails_reconstruction(tmp_path, change):
    binding, cards, payload, _ = fixture_data()
    archive = ContextArchive(ArtifactStore(tmp_path))
    _, record = build_context(
        "json", payload, cards, binding, {"E002"}, archive, mode="structured", cap=12000
    )
    summary = archive.store.read_json(record["summary_artifact"]["sha256"])
    if change == "negation":
        summary["entries"][1]["payload"]["text"] = "SYNTHETIC universal safety"
    elif change == "version":
        summary["entries"][1]["source_version_ids"] = ["b" * 64]
    else:
        summary["snapshot_sha256"] = "b" * 64
    forged = archive.store.put_json(summary)
    with pytest.raises((ArtifactIntegrityError, FileNotFoundError)):
        restore_summary(
            archive.store, forged, binding, expected_snapshot=record["snapshot_artifact"]["sha256"]
        )


def test_missing_counter_and_corrupted_card_cannot_be_summarized():
    binding, cards, payload, _ = fixture_data()
    snapshot = {
        "binding": binding.model_dump(mode="json"),
        "cards": [c.model_dump(mode="json") for c in cards],
        "payload": payload,
        "counter_aliases": ["E999"],
    }
    with pytest.raises(ArtifactIntegrityError, match="counter"):
        summarize(snapshot, H)
    snapshot["counter_aliases"] = []
    snapshot["cards"][0]["payload"]["text"] = "fake"
    with pytest.raises(ArtifactIntegrityError, match="identity"):
        summarize(snapshot, H)


def test_unlabelled_negation_and_scope_are_protected_without_model_counter_tag(tmp_path):
    binding, cards, payload, _ = fixture_data()
    value, _ = build_context(
        "json",
        payload,
        cards,
        binding,
        set(),
        ContextArchive(ArtifactStore(tmp_path)),
        mode="structured",
        cap=12000,
    )
    assert "does NOT establish" in json_text(value)
    assert "Model Year 2010" in json_text(value)


def test_dedup_preserves_equivalent_aliases_and_different_source_versions():
    binding, cards, payload, _ = fixture_data()
    body = cards[0].model_dump(mode="json", exclude={"evidence_id"})
    body["origin_sha256"] = "b" * 64
    duplicate = EvidenceCard.model_validate(body | {"evidence_id": content_hash(body)})
    other = deepcopy(body)
    other["source_version_ids"] = ["c" * 64]
    different = EvidenceCard.model_validate(other | {"evidence_id": content_hash(other)})
    snapshot = {
        "binding": binding.model_dump(mode="json"),
        "cards": [c.model_dump(mode="json") for c in [cards[0], duplicate, different]],
        "payload": payload,
        "counter_aliases": [],
    }
    summary = summarize(snapshot, H)
    assert len(summary.entries) == 2 and summary.entries[0].aliases == ["E001", "E002"]


def test_mandatory_context_overflow_stops_without_clipping_counter(tmp_path):
    binding, cards, payload, _ = fixture_data()
    payload["initial_plan"]["goal"] = "x" * 20000
    archive = ContextArchive(ArtifactStore(tmp_path))
    with pytest.raises(ModelCallFailed, match="prompt_budget"):
        build_context(
            "json", payload, cards, binding, {"E002"}, archive, mode="structured", cap=12000
        )
    assert any(v["label"] == "complete_context_snapshot" for v in archive.handles.values())


def test_pages_roundtrip_reject_unknown_handles_and_keep_evidence_identity(tmp_path):
    binding, cards, payload, aliases = fixture_data()
    archive = ContextArchive(ArtifactStore(tmp_path))
    handle, _ = archive.add({"source": "SYNTHETIC 一二三 NOT safe"}, "test")
    pages = []
    offset = 0
    while True:
        page = archive.read(handle, offset, 7)
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pages) == json_text({"source": "SYNTHETIC 一二三 NOT safe"})
    with pytest.raises(ValueError):
        archive.read("A999")
    with pytest.raises(ValueError):
        text_page("abc", 4, 1)
    with pytest.raises(ValueError):
        text_page("abc", 0, 4001)
    original = evidence_page([c.model_dump(mode="json") for c in cards], aliases, "E002", 0, 10)
    assert (
        original["partial"]
        and original["source_version_ids"] == [H]
        and original["required_context"] == ["E001"]
    )


@pytest.mark.parametrize(
    "value",
    [
        decision("read"),
        decision("read", artifact_id="../secret"),
        decision("read", artifact_id="A001", evidence_alias="E001"),
        decision("read", artifact_id="A001", offset=-1),
        decision("search", query="brake", artifact_id="A001"),
    ],
)
def test_read_decision_cannot_address_unregistered_paths_or_mixed_arguments(value):
    with pytest.raises(ValidationError):
        Decision.model_validate(value)

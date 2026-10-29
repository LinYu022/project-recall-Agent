"""Extractive context: immutable complete blocks, explicit omissions and revision-bound recovery."""

import json
import re
from time import perf_counter
from typing import Annotated, Literal

from pydantic import Field

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from ingestion.records import content_hash
from investigation.contracts import Alias, EvidenceCard
from investigation.flow import prompt_cards, redact
from models.chat_api import ModelCallFailed
from runtime.artifacts import ArtifactIntegrityError


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def prompt_size(system, payload):
    # Match RunBudget exactly. This is a character proxy, not a tokenizer or cache hit.
    return len(system + json.dumps(payload, ensure_ascii=False))


class ContextBinding(Contract):
    task_id: Text
    release_id: Digest
    task_spec_sha256: Digest
    plan_sha256: Digest
    revision_id: Text | None = None
    authorization_id: Text | None = None
    epoch: Annotated[int, Field(strict=True, ge=0)] = 0


class SummaryEntry(Contract):
    aliases: Annotated[list[Alias], Field(min_length=1)]
    kind: Literal["text", "count", "relationship", "structured_lookup"]
    scope_channel: Text
    source_ids: list[Text]
    source_version_ids: list[Digest]
    required_context: list[Alias]
    permitted_claim_kinds: list[Text]
    payload: dict
    protected_conditions_or_negations: bool


class StructuredSummary(Contract):
    schema_version: Literal["extractive-context-summary/0.1"] = "extractive-context-summary/0.1"
    binding: ContextBinding
    snapshot_sha256: Digest
    entries: list[SummaryEntry]
    counter_aliases: list[Alias]
    current_hypotheses: list[dict]
    unresolved_conditions: list[str]
    compression_policy: Literal["exact_blocks_deduplicated_no_sentence_clipping"] = (
        "exact_blocks_deduplicated_no_sentence_clipping"
    )
    model_calls: Literal[0] = 0
    provider_usage: None = None
    estimated_cost: None = None


def summarize(snapshot, snapshot_sha256):
    cards = [EvidenceCard.model_validate(c) for c in snapshot["cards"]]
    aliases, visible = prompt_cards(cards)
    grouped: dict[str, dict] = {}
    for card, view in zip(cards, visible, strict=True):
        if content_hash(card.model_dump(mode="json", exclude={"evidence_id"})) != card.evidence_id:
            raise ArtifactIntegrityError("summary card identity differs")
        if (
            card.release_id != snapshot["binding"]["release_id"]
            or card.task_id != snapshot["binding"]["task_id"]
        ):
            raise ArtifactIntegrityError("summary card binding differs")
        # Source versions and exact locators are part of the dedup key. Similar text is not merged.
        stable = {k: view[k] for k in ("kind", "scope_channel", "payload")}
        stable |= {"source_ids": card.source_ids, "source_version_ids": card.source_version_ids}
        key = content_hash(stable)
        if key not in grouped:
            grouped[key] = stable | {
                "aliases": [],
                "required_context": [],
                "permitted_claim_kinds": view["permitted_claim_kinds"],
                "protected_conditions_or_negations": bool(
                    re.search(
                        r"\b(not|no|never|without|only|unless|unknown|unconfirmed|cannot|may|if|certain|subject|conditions?|19\d{2}|20\d{2})\b|不|否|未|仅|条件|年款|范围",
                        view["payload"].get("text", ""),
                        re.IGNORECASE,
                    )
                ),
            }
        group = grouped[key]
        group["aliases"].append(view["alias"])
        group["required_context"] = sorted(
            set(group["required_context"]) | set(view["required_context"])
        )
    if not set(snapshot["counter_aliases"]) <= set(aliases):
        raise ArtifactIntegrityError("summary counter evidence is missing")
    hypotheses = {}
    for revision in snapshot["payload"]["previous_decisions"]:
        for hypothesis in revision["hypotheses"]:
            hypotheses[hypothesis["hypothesis_id"]] = hypothesis
    revisions = snapshot["payload"]["previous_decisions"]
    return StructuredSummary(
        binding=ContextBinding.model_validate(snapshot["binding"]),
        snapshot_sha256=snapshot_sha256,
        entries=[SummaryEntry.model_validate(g) for g in grouped.values()],
        counter_aliases=sorted(snapshot["counter_aliases"]),
        current_hypotheses=list(hypotheses.values()),
        # No verified resolution protocol exists yet: dropping an old gap is not resolution.
        unresolved_conditions=list(dict.fromkeys(g for r in revisions for g in r["evidence_gaps"])),
    )


def restore_summary(store, reference, expected_binding, *, expected_snapshot):
    """Rebuild from pinned inputs, then compare; a self-consistent summary hash is insufficient."""
    value = StructuredSummary.model_validate(store.read_json(reference["sha256"]))
    if value.binding != expected_binding:
        raise ArtifactIntegrityError("context revision authorization or release changed")
    if value.snapshot_sha256 != expected_snapshot:
        raise ArtifactIntegrityError("summary snapshot changed")
    snapshot = store.read_json(value.snapshot_sha256)
    if snapshot["binding"] != expected_binding.model_dump(mode="json"):
        raise ArtifactIntegrityError("snapshot binding differs")
    rebuilt = summarize(snapshot, value.snapshot_sha256)
    if rebuilt != value:
        raise ArtifactIntegrityError("summary differs from pinned evidence or history")
    return value


def text_page(text, offset=0, limit=2000):
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 4000:
        raise ValueError("invalid page range")
    if offset > len(text):
        raise ValueError("page offset outside artifact")
    end = min(len(text), offset + limit)
    return {
        "offset": offset,
        "total_characters": len(text),
        "next_offset": end if end < len(text) else None,
        "partial": offset != 0 or end != len(text),
        "text": text[offset:end],
        "page_is_navigation_not_new_evidence": True,
    }


class ContextArchive:
    """Only server-registered handles can be read. A model cannot supply paths or digests."""

    def __init__(self, store):
        self.store = store
        self.handles: dict[str, dict] = {}

    def add(self, value, label):
        ref = self.store.put_json(value)
        for handle, item in self.handles.items():
            if item["sha256"] == ref["sha256"]:
                return handle, ref
        handle = f"A{len(self.handles) + 1:03}"
        self.handles[handle] = ref | {"label": label}
        return handle, ref

    def read(self, handle, offset=0, limit=2000):
        if handle not in self.handles:
            raise ValueError("unknown_context_artifact_handle")
        return {"artifact_id": handle} | text_page(
            redact(json_text(self.store.read_json(self.handles[handle]["sha256"]))), offset, limit
        )


def evidence_page(cards, aliases, alias, offset=0, limit=2000):
    if alias not in aliases:
        raise ValueError("unknown evidence alias")
    card = next(c for c in cards if c["evidence_id"] == aliases[alias])
    text = card["payload"].get("text")
    return {
        "alias": alias,
        "kind": card["kind"],
        "source_ids": card["source_ids"],
        "source_version_ids": card["source_version_ids"],
        "required_context": [a for a, i in aliases.items() if i in card["required_context_ids"]],
        "citations": [
            {k: c[k] for k in ("source_version_id", "unit_id", "start", "end", "quote_sha256")}
            for c in card["payload"].get("citations", [])
        ],
    } | text_page(redact(text if text is not None else json_text(card["payload"])), offset, limit)


def build_context(system, payload, cards, binding, counters, archive, *, mode, cap):
    """Select whole dependency closures; pinned scope/counters never lose a budget contest."""
    started = perf_counter()
    snapshot = {
        "binding": binding.model_dump(mode="json"),
        "payload": payload,
        "cards": [c.model_dump(mode="json") for c in cards],
        "counter_aliases": sorted(counters),
    }
    handle, snap_ref = archive.add(snapshot, "complete_context_snapshot")
    summary = summarize(snapshot, snap_ref["sha256"])
    summary_handle, summary_ref = archive.add(summary.model_dump(mode="json"), "structured_summary")
    restore_summary(archive.store, summary_ref, binding, expected_snapshot=snap_ref["sha256"])
    journal, _ = archive.add(
        {k: payload[k] for k in ("observations", "previous_decisions")}, "complete_journal"
    )
    evidence_index, _ = archive.add(
        {"entries": [e.model_dump(mode="json", exclude={"payload"}) for e in summary.entries]},
        "evidence_index",
    )
    candidates_handle, _ = archive.add({"candidates": payload["candidates"]}, "all_candidates")
    if mode == "full_history":
        result = payload
    elif mode == "recent_window":
        # Deliberately naive ablation, never the production default.
        result = payload | {
            "evidence": payload["evidence"][-2:],
            "observations": payload["observations"][-2:],
            "previous_decisions": payload["previous_decisions"][-1:],
        }
    else:
        entries = summary.entries
        by_alias = {a: i for i, e in enumerate(entries) for a in e.aliases}

        def closure(indices):
            required = set(indices)
            while True:
                expanded = required | {
                    by_alias[a] for i in required for a in entries[i].required_context
                }
                if expanded == required:
                    return required
                required = expanded

        pinned = closure(
            {
                i
                for i, e in enumerate(entries)
                if e.kind != "text" or e.protected_conditions_or_negations
            }
            | {by_alias[a] for a in counters}
        )

        def render(selected):
            visible = []
            for i in sorted(selected):
                entry = entries[i]
                value = entry.model_dump(mode="json", exclude={"aliases"})
                visible.append(
                    value | {"alias": entry.aliases[0], "equivalent_aliases": entry.aliases}
                )
            return payload | {
                "evidence": visible,
                "candidates": payload["candidates"][-6:],
                "observations": payload["observations"][-2:],
                "previous_decisions": payload["previous_decisions"][-1:],
                "context_summary": {
                    "version": summary.schema_version,
                    "current_hypotheses": summary.current_hypotheses,
                    "unresolved_conditions": summary.unresolved_conditions,
                    "counter_aliases": summary.counter_aliases,
                    "omitted_evidence_groups": len(entries) - len(selected),
                    "complete_snapshot": handle,
                    "summary_artifact": summary_handle,
                    "complete_journal": journal,
                    "evidence_index": evidence_index,
                    "all_candidates": candidates_handle,
                    "omissions_are_not_negative_findings": True,
                    "complete_blocks_preserve_conditions_and_negations": True,
                },
            }

        selected = set(pinned)
        result = render(selected)
        if prompt_size(system, result) > cap:
            raise ModelCallFailed("prompt_budget_exhausted")
        words = set(re.findall(r"[a-z0-9]+", payload["question"].lower()))
        priority = sorted(
            range(len(entries)),
            key=lambda i: (
                len(words & set(re.findall(r"[a-z0-9]+", json_text(entries[i].payload).lower()))),
                i,
            ),
            reverse=True,
        )
        for i in priority:
            candidate = closure(selected | {i})
            trial = render(candidate)
            if prompt_size(system, trial) <= cap:
                selected, result = candidate, trial
    metadata = {
        "mode": mode,
        "snapshot_artifact": snap_ref,
        "summary_artifact": summary_ref,
        "before_characters": prompt_size(system, payload),
        "after_characters": prompt_size(system, result),
        "summary_model_calls": 0,
        "summary_provider_usage": None,
        "summary_estimated_cost": None,
        "summary_method": "deterministic_extractive",
        "cache_used_for_selection": False,
        "context_build_elapsed_ms": (perf_counter() - started) * 1000,
    }
    return result, metadata

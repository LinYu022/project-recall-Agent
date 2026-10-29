"""Build evidence documents only from verified chunks and fixed metadata rules."""

import json
from pathlib import Path

from entities.normalize import EntityRules, project_record
from entities.scopes import ScopeRules
from ingestion.records import content_hash, prepare_import
from ingestion.temporal import TemporalRequest, select_versions
from parsing.chunk_contracts import (
    ChunkConfig,
    StructureRules,
    load_chunk_config,
    load_structure_rules,
)
from parsing.chunks import build_chunks
from parsing.citations import resolve_citation
from parsing.contracts import Citation, ParseRequest
from parsing.parser import parse_source, text_hash
from retrieval.contracts import BuildRequest
from runtime.artifacts import ArtifactStore


def build_corpus(
    raw_root: Path,
    parsed_root: Path,
    chunk_root: Path,
    request: BuildRequest,
    entity_rules: EntityRules,
    scope_rules: ScopeRules,
    pdf_rules: dict,
    *,
    chunk_config: ChunkConfig | None = None,
    structure_rules: StructureRules | None = None,
) -> dict:
    temporal = select_versions(
        raw_root, TemporalRequest(snapshot_ids=request.snapshot_ids, time=request.time)
    )
    if temporal["status"] not in {"selected", "empty"}:
        raise ValueError("retrieval build requires a complete fixed temporal selection")
    selected = {v["version_id"] for v in temporal["selected"]}
    seen = set()
    documents = []
    config = chunk_config or load_chunk_config(request.chunk_strategy)
    structure = structure_rules or load_structure_rules()
    if config.strategy != request.chunk_strategy:
        raise ValueError("chunk recipe differs from requested strategy")
    for snapshot_id in sorted(request.snapshot_ids):
        batch = prepare_import(raw_root, snapshot_id)
        for row in batch.rows:
            version = row.version
            if (
                version is None
                or version["version_id"] not in selected
                or version["version_id"] in seen
            ):
                continue
            seen.add(version["version_id"])
            parsed = parse_source(
                raw_root, ParseRequest(snapshot_id=snapshot_id, ordinal=row.ordinal)
            )
            parsed_ref = ArtifactStore(parsed_root).put_json(parsed)
            chunks = build_chunks(raw_root, parsed_root, parsed_ref["sha256"], config, structure)
            chunk_ref = ArtifactStore(chunk_root).put_json(chunks)
            payload = version["payload"]
            projection = project_record(version, entity_rules)
            vehicles = []
            matching_facts = [
                f for f in scope_rules.facts if f.source_version_id == version["version_id"]
            ]
            # Facts are verified against raw JSON before accepting their canonical values.
            for fact in matching_facts:
                if (
                    not isinstance(payload.get(fact.field), str)
                    or payload[fact.field].count(fact.quote) != 1
                ):
                    raise ValueError("reviewed source fact no longer matches")
            for vehicle in projection["vehicles"]:
                conditions = {
                    f.key: str(f.value).upper()
                    for f in matching_facts
                    if f.facet == "configuration"
                }
                ranges = [
                    f.value.model_dump(mode="json")
                    for f in matching_facts
                    if f.facet == "production_range" and not isinstance(f.value, str)
                ]
                vehicles.append(
                    {k: vehicle[k]["canonical"] for k in ("make", "model", "model_year")}
                    | {
                        "production_range": ranges[0]
                        if ranges and len(projection["vehicles"]) == 1
                        else None,
                        "configurations": conditions if len(projection["vehicles"]) == 1 else {},
                    }
                )
            metadata = {
                "source_id": batch.snapshot.source.source_id,
                "source_kind": version["source_kind"],
                "source_version_id": version["version_id"],
                "campaign_number": payload.get("NHTSACampaignNumber"),
                "odi_number": payload.get("odiNumber"),
                "vehicles": vehicles,
                "components": [c["canonical"] for c in projection["components"]],
                "official_references": [
                    f.value for f in matching_facts if f.facet == "official_reference"
                ],
            }
            if version["source_kind"] == "investigation_pdf":
                rule = next(
                    (
                        r
                        for r in pdf_rules["documents"]
                        if r["source_version_id"] == version["version_id"]
                    ),
                    None,
                )
                if rule is not None:
                    if not set(rule["metadata"]) <= {
                        "vehicles",
                        "components",
                        "official_references",
                        "investigation_id",
                    }:
                        raise ValueError("PDF metadata rules cannot overwrite source identity")
                    for citation in rule["citations"]:
                        checked = Citation.model_validate(citation)
                        if (
                            checked.source_version_id != version["version_id"]
                            or checked.parse_artifact_sha256 != parsed_ref["sha256"]
                        ):
                            raise ValueError("PDF metadata citation belongs to another version")
                        resolve_citation(raw_root, parsed_root, checked)
                    metadata.update(rule["metadata"])
            for node in chunks["chunks"]:
                if not node["searchable"]:
                    continue
                field = node["ranges"][0]["field_pointer"]
                prefix = "\n".join(
                    [
                        str(
                            metadata.get("campaign_number")
                            or metadata.get("odi_number")
                            or metadata.get("investigation_id")
                            or ""
                        ),
                        *[
                            " ".join(
                                str(v.get(k) or "unknown") for k in ("make", "model", "model_year")
                            )
                            + " "
                            + json.dumps(v.get("configurations", {}), sort_keys=True)
                            for v in metadata["vehicles"]
                        ],
                        ", ".join(c for c in metadata["components"] if c),
                        ", ".join(metadata["official_references"]),
                    ]
                )
                # Identical source information is available to every retrieval method.
                text = prefix + "\n" + " / ".join(node["title_path"]) + "\n" + node["text"]
                item = {
                    "chunk_id": node["chunk_id"],
                    "chunk_artifact_sha256": chunk_ref["sha256"],
                    "parse_artifact_sha256": parsed_ref["sha256"],
                    "source": chunks["source"],
                    "metadata": metadata,
                    "field": field.rsplit("/", 1)[-1] if field else node["kind"],
                    "text": node["text"],
                    "search_text": text,
                    "search_text_sha256": text_hash(text),
                    "ranges": node["ranges"],
                }
                parent = next(n for n in chunks["chunks"] if n["chunk_id"] == node["parent_id"])
                item["evidence_key"] = content_hash(
                    [
                        version["source_kind"],
                        metadata["campaign_number"] or version["version_id"],
                        item["field"],
                        node["text_sha256"],
                        parent["text_sha256"],
                    ]
                )
                documents.append(item | {"document_id": content_hash(item)})
    recipe = {
        "request": request.model_dump(mode="json"),
        "temporal_selection_sha256": temporal["selection_sha256"],
        "entity_rules": entity_rules.model_dump(mode="json"),
        "scope_rules_sha256": scope_rules.sha256,
        "pdf_rules_sha256": content_hash(pdf_rules),
        "chunk_config": config.model_dump(mode="json"),
        "structure_rules_sha256": content_hash(structure.model_dump(mode="json")),
        "engine_version": "retrieval-corpus/0.1",
        "duplicate_policy": "filter_then_identical_campaign_field_leaf_and_parent/0.1",
        "input_set_sha256": temporal["input_set_sha256"],
    }
    documents.sort(key=lambda d: d["document_id"])
    build_id = content_hash(
        {"recipe": recipe, "document_ids": [d["document_id"] for d in documents]}
    )
    return {
        "schema_version": "retrieval-corpus/0.1",
        "dataset_build_id": build_id,
        "recipe": recipe,
        "documents": documents,
        "dataset_release": None,
        "semantic_support": "not_evaluated",
    }

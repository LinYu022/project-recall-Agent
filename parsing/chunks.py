"""Two replayable chunk recipes with explicit parents and required context."""

import re
from pathlib import Path

from ingestion.records import content_hash
from parsing.chunk_contracts import ChunkConfig, StructureRules, TextRange, fingerprint
from parsing.citations import checked_parse, make_citation
from parsing.contracts import ParseRequest
from parsing.parser import source_record, text_hash
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


class ChunkBuildError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ranges(parsed: dict, ranges: list[TextRange]) -> list[dict]:
    units = {unit["unit_id"]: unit for unit in parsed["units"]}
    order = {identifier: index for index, identifier in enumerate(units)}
    result = []
    previous = (-1, -1)
    for part in ranges:
        unit = units.get(part.unit_id)
        if unit is None or not 0 <= part.start < part.end <= len(unit["text"]):
            raise ChunkBuildError(
                "chunk_range_invalid", "structure range is outside the fixed parsed unit"
            )
        if text_hash(unit["text"][part.start : part.end]) != part.text_sha256:
            raise ArtifactIntegrityError("structure range text has changed")
        if (order[part.unit_id], part.start) < previous:
            raise ChunkBuildError(
                "chunk_range_order_invalid",
                "source ranges must be in document order without overlap",
            )
        previous = (order[part.unit_id], part.end)
        result.append(
            part.model_dump(mode="json")
            | {
                "page_number": unit["page_number"],
                "field_pointer": unit["field_pointer"],
                "region_ids": [
                    r["region_id"]
                    for r in unit["regions"]
                    if r["start"] < part.end and part.start < r["end"]
                ],
            }
        )
    return result


def _span(unit: dict, start: int, end: int) -> TextRange:
    return TextRange(
        unit_id=unit["unit_id"],
        start=start,
        end=end,
        text_sha256=text_hash(unit["text"][start:end]),
    )


def _table(text: str, template: dict, base: dict) -> dict:
    """One reviewed header/row/footnote template; reject unfamiliar cells instead of guessing."""
    lines = text.splitlines(keepends=True)
    columns, labels = template["columns"], template["row_labels"]
    if not lines or lines[0].split() != columns or len(lines) != len(labels) + 2:
        raise ChunkBuildError(
            "chunk_table_shape_changed", "reviewed table header or row count no longer matches"
        )
    cells, rows, cursor = [], [], len(lines[0])
    number = r"(0|[1-9][0-9]{0,2}(?:,[0-9]{3})+|[1-9][0-9]*)"
    for label, line in zip(labels, lines[1:-1], strict=True):
        pattern = r"\s*" + re.escape(label) + r"\s+" + r"\s+".join([number] * len(columns)) + r"\s*"
        match = re.fullmatch(pattern, line)
        if match is None:
            raise ChunkBuildError(
                "chunk_table_values_unsupported",
                "reviewed table row is ambiguous or contains unsupported values",
            )
        values = []
        for index, column in enumerate(columns, 1):
            raw = match.group(index)
            values.append(int(raw.replace(",", "")))
            cells.append(
                {
                    "row": label,
                    "column": column,
                    "raw": raw,
                    "value": values[-1],
                    "unit_id": base["unit_id"],
                    "start": base["start"] + cursor + match.start(index),
                    "end": base["start"] + cursor + match.end(index),
                }
            )
        rows.append({"label": label, "values": values})
        cursor += len(line)
    footnote = lines[-1].strip()
    if (
        not footnote.startswith(template["footnote_prefix"])
        or footnote == template["footnote_prefix"]
    ):
        raise ChunkBuildError("chunk_table_footnote_missing", "reviewed table footnote is missing")
    return {
        "columns": columns,
        "rows": rows,
        "cells": cells,
        "footnote": footnote,
        "method": "reviewed_template_exact_rows_not_general_table_detection",
    }


def build_chunks(
    raw_root: Path, parsed_root: Path, digest: str, config: ChunkConfig, rules: StructureRules
) -> dict:
    parsed = checked_parse(raw_root, parsed_root, digest)
    if parsed["status"] != "parsed":
        raise ChunkBuildError(
            "chunk_incomplete_parse", "complete parsed text is required before chunking"
        )
    candidates = [
        d
        for d in rules.documents
        if d.source_version_id == parsed["source"]["source_version_id"]
        and d.parser_sha256 == parsed["parser_sha256"]
    ]
    profile = candidates[0] if candidates else None
    pdf = parsed["source"]["source_kind"] == "investigation_pdf"
    if pdf and config.strategy == "structure" and profile is None:
        raise ChunkBuildError(
            "pdf_layout_not_reviewed", "PDF structure requires a matching reviewed layout profile"
        )
    units = {unit["unit_id"]: unit for unit in parsed["units"]}
    recipe = {
        "engine_version": "chunk-builder/0.1",
        "expansion_policy": "immediate_parent_and_recursive_required_context/0.1",
        "window_unit": "unicode_codepoints_within_page_or_field",
        "table_policy": "atomic_header_rows_and_footnote",
        "config": config.model_dump(mode="json"),
        "structure_rules": rules.model_dump(mode="json"),
        "config_sha256": fingerprint(config),
        "structure_rules_sha256": fingerprint(rules),
    }
    binding = {
        "parse_artifact_sha256": digest,
        "source_version_id": parsed["source"]["source_version_id"],
        "recipe_sha256": content_hash(recipe),
    }
    chunks: list[dict] = []

    def add(
        kind: str,
        ranges: list[TextRange],
        parent: str | None,
        titles: list[str],
        *,
        searchable=False,
        context: list[str] | None = None,
        table=None,
    ) -> dict:
        locations = _ranges(parsed, ranges)
        text = "\n\n".join(units[r["unit_id"]]["text"][r["start"] : r["end"]] for r in locations)
        node = binding | {
            "kind": kind,
            "parent_id": parent,
            "title_path": titles,
            "searchable": searchable,
            "required_context_ids": context or [],
            "ranges": locations,
            "text": text,
            "text_sha256": text_hash(text),
            "text_join_policy": "double_newline_between_ranges_cite_each_source_range",
            "table": table,
        }
        node = node | {"chunk_id": content_hash(node)}
        chunks.append(node)
        return node

    root = add("document", [_span(u, 0, len(u["text"])) for u in parsed["units"]], None, [])
    sections: dict[str, dict] = {}
    occupied: list[dict] = []
    if profile is not None:
        # Context edges are a DAG. Resolve forward references before hashing each parent.
        remaining = list(profile.sections)
        while remaining:
            for section in remaining[:]:
                if not all(key in sections for key in section.context_keys):
                    continue
                context = [sections[key]["chunk_id"] for key in section.context_keys]
                sections[section.key] = add(
                    "section", section.ranges, root["chunk_id"], section.title_path, context=context
                )
                for location in sections[section.key]["ranges"]:
                    if any(
                        location["unit_id"] == other["unit_id"]
                        and location["start"] < other["end"]
                        and other["start"] < location["end"]
                        for other in occupied
                    ):
                        raise ChunkBuildError(
                            "chunk_sections_overlap", "reviewed sections must not overlap"
                        )
                    occupied.append(location)
                remaining.remove(section)
    if config.strategy == "structure" and pdf:
        assert profile is not None
        for section in profile.sections:
            parent = sections[section.key]
            part_locations: list[dict] = []
            for part in section.parts:
                locations = _ranges(parsed, part.ranges)
                for location in locations:
                    if not any(
                        p["unit_id"] == location["unit_id"]
                        and p["start"] <= location["start"]
                        and location["end"] <= p["end"]
                        for p in parent["ranges"]
                    ):
                        raise ChunkBuildError(
                            "chunk_child_outside_parent",
                            "child range must be inside its section parent",
                        )
                    if any(
                        p["unit_id"] == location["unit_id"]
                        and location["start"] < p["end"]
                        and p["start"] < location["end"]
                        for p in part_locations
                    ):
                        raise ChunkBuildError(
                            "chunk_children_overlap", "reviewed child parts must not overlap"
                        )
                    part_locations.append(location)
                table = None
                if part.table:
                    r = locations[0]
                    table = _table(
                        units[r["unit_id"]]["text"][r["start"] : r["end"]],
                        part.table.model_dump(mode="json"),
                        r,
                    )
                add(
                    part.kind,
                    part.ranges,
                    parent["chunk_id"],
                    section.title_path,
                    searchable=True,
                    context=parent["required_context_ids"],
                    table=table,
                )
    else:
        unit_parents = {
            unit["unit_id"]: add(
                "unit",
                [_span(unit, 0, len(unit["text"]))],
                root["chunk_id"],
                [unit["field_pointer"] or f"PDF page {unit['page_number']}"],
            )
            for unit in parsed["units"]
        }
        for unit in parsed["units"]:
            parent = unit_parents[unit["unit_id"]]
            whole = (
                parsed["source"]["source_kind"] == "complaint_api"
                and len(unit["text"]) <= config.short_complaint_characters
            )
            if whole:
                windows = [(0, len(unit["text"]))]
            elif config.strategy == "structure":
                windows = [
                    (match.start(), match.end())
                    for match in re.finditer(r"\S.*?(?=\n[ \t]*\n|\Z)", unit["text"], re.S)
                ]
            else:
                assert config.fixed_characters is not None and config.fixed_overlap is not None
                width, step = (
                    config.fixed_characters,
                    config.fixed_characters - config.fixed_overlap,
                )
                windows = []
                start = 0
                while start < len(unit["text"]):
                    end = min(start + width, len(unit["text"]))
                    windows.append((start, end))
                    if end == len(unit["text"]):
                        break
                    start += step
            for start, end in windows:
                if not unit["text"][start:end].strip():
                    continue
                # Both strategies have the same source scope/context available. Fixed windows keep
                # their original boundaries; required context is attached without rewriting text.
                context = []
                if parsed["source"]["source_kind"] == "recall_api":
                    context.extend(
                        p["chunk_id"] for key, p in unit_parents.items() if key != unit["unit_id"]
                    )
                if profile is not None:
                    for section in profile.sections:
                        if any(
                            r.unit_id == unit["unit_id"] and r.start < end and start < r.end
                            for r in section.ranges
                        ):
                            context.extend(
                                sections[key]["chunk_id"] for key in section.context_keys
                            )
                add(
                    "whole_narrative"
                    if whole
                    else "fixed_window"
                    if config.strategy == "fixed"
                    else "narrative",
                    [_span(unit, start, end)],
                    parent["chunk_id"],
                    parent["title_path"],
                    searchable=True,
                    context=sorted(set(context)),
                )
    _, version = source_record(
        raw_root,
        ParseRequest(
            snapshot_id=parsed["source"]["snapshot_id"], ordinal=parsed["source"]["ordinal"]
        ),
    )
    fields = (
        ("products", "components")
        if not pdf and parsed["source"]["source_kind"] == "complaint_api"
        else ("Make", "Model", "ModelYear", "Component")
        if not pdf
        else ()
    )
    return {
        "schema_version": "chunk-set/0.1",
        "source": parsed["source"],
        **binding,
        "recipe": recipe,
        "chunks": chunks,
        "root_chunk_id": root["chunk_id"],
        "source_record_context": {key: version["payload"].get(key) for key in fields},
        "context_status": "reviewed_layout_context"
        if profile
        else "source_record_fields"
        if not pdf
        else "layout_not_reviewed",
        "dataset_release": None,
        "claim_support": "not_evaluated",
    }


def checked_chunks(raw_root: Path, parsed_root: Path, chunk_root: Path, digest: str) -> dict:
    saved = ArtifactStore(chunk_root).read_json(digest)
    if saved.get("schema_version") != "chunk-set/0.1":
        raise ValueError("unsupported chunk set schema")
    recipe = saved["recipe"]
    replayed = build_chunks(
        raw_root,
        parsed_root,
        saved["parse_artifact_sha256"],
        ChunkConfig.model_validate(recipe["config"]),
        StructureRules.model_validate(recipe["structure_rules"]),
    )
    if replayed != saved:
        raise ArtifactIntegrityError("chunk set differs from its source/recipe replay")
    return saved


def expand_chunk(chunk_set: dict, chunk_id: str) -> dict:
    nodes = {node["chunk_id"]: node for node in chunk_set["chunks"]}
    if chunk_id not in nodes:
        raise ValueError("chunk is not part of this set")
    pending = [chunk_id]
    seen = set()
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        node = nodes[identifier]
        pending.extend(node["required_context_ids"])
        # Preserve the complete immediate parent, then follow only explicit context links.
        # The whole-document root remains navigable without expanding every long PDF by default.
        if identifier == chunk_id and node["parent_id"] is not None:
            pending.append(node["parent_id"])
    return {
        "focus": nodes[chunk_id],
        "context": [node for node in chunk_set["chunks"] if node["chunk_id"] in seen - {chunk_id}],
        "source_record_context": chunk_set["source_record_context"],
        "interpretation": "context_is_available_not_proof_that_all_constraints_are_complete",
    }


def cite_chunk(raw_root: Path, parsed_root: Path, chunk_set: dict, chunk_id: str) -> list[dict]:
    node = expand_chunk(chunk_set, chunk_id)["focus"]
    parsed = checked_parse(raw_root, parsed_root, chunk_set["parse_artifact_sha256"])
    indexes = {unit["unit_id"]: index for index, unit in enumerate(parsed["units"])}
    return [
        make_citation(
            raw_root,
            parsed_root,
            chunk_set["parse_artifact_sha256"],
            indexes[r["unit_id"]],
            r["start"],
            r["end"],
        ).model_dump(mode="json")
        for r in node["ranges"]
    ]

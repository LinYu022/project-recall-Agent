"""Reviewed structure and chunk recipes are data, never execution instructions."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from ingestion.records import content_hash


class TextRange(Contract):
    unit_id: Digest
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, gt=0)]
    text_sha256: Digest

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("text range must be nonempty and ordered")
        return self


class TableTemplate(Contract):
    columns: Annotated[list[Text], Field(min_length=1)]
    row_labels: Annotated[list[Text], Field(min_length=1)]
    footnote_prefix: Text

    @model_validator(mode="after")
    def unique_labels(self):
        if len(set(self.columns)) != len(self.columns) or len(set(self.row_labels)) != len(
            self.row_labels
        ):
            raise ValueError("table template labels must be unique")
        return self


class StructurePart(Contract):
    kind: Literal["paragraph", "metadata", "table"]
    ranges: Annotated[list[TextRange], Field(min_length=1)]
    table: TableTemplate | None = None

    @model_validator(mode="after")
    def table_has_template(self):
        if (self.kind == "table") != (self.table is not None):
            raise ValueError("table parts require a reviewed template")
        if self.table is not None and len(self.ranges) != 1:
            raise ValueError("table template v0.1 requires one source range")
        return self


class SectionRule(Contract):
    key: Text
    title_path: Annotated[list[Text], Field(min_length=1)]
    ranges: Annotated[list[TextRange], Field(min_length=1)]
    context_keys: list[Text] = []
    parts: Annotated[list[StructurePart], Field(min_length=1)]


class DocumentStructure(Contract):
    source_version_id: Digest
    parser_sha256: Digest
    review_note: Text
    sections: Annotated[list[SectionRule], Field(min_length=1)]

    @model_validator(mode="after")
    def acyclic_context(self):
        graph = {s.key: s.context_keys for s in self.sections}
        if len(graph) != len(self.sections):
            raise ValueError("section keys must be unique")
        visited, active = set(), set()

        def visit(key):
            if key not in graph or key in active:
                raise ValueError("unknown or cyclic required context")
            if key not in visited:
                active.add(key)
                for dependency in graph[key]:
                    visit(dependency)
                active.remove(key)
                visited.add(key)

        for key in graph:
            visit(key)
        return self


class StructureRules(Contract):
    rules_version: Text
    documents: list[DocumentStructure]

    @model_validator(mode="after")
    def unique_versions(self):
        keys = [(d.source_version_id, d.parser_sha256) for d in self.documents]
        if len(set(keys)) != len(keys):
            raise ValueError("document structure entries must be unique")
        return self


class ChunkConfig(Contract):
    schema_version: Literal["chunk-config/0.1"] = "chunk-config/0.1"
    config_version: Text
    strategy: Literal["structure", "fixed"]
    short_complaint_characters: Annotated[int, Field(strict=True, ge=1, le=100_000)] = 4000
    fixed_characters: Annotated[int, Field(strict=True, ge=1, le=100_000)] | None = None
    fixed_overlap: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if self.strategy == "fixed":
            if (
                self.fixed_characters is None
                or self.fixed_overlap is None
                or self.fixed_overlap >= self.fixed_characters
            ):
                raise ValueError("fixed windows require 0 <= overlap < characters")
        elif self.fixed_characters is not None or self.fixed_overlap is not None:
            raise ValueError("structured chunks do not use fixed-window parameters")
        return self


def load_structure_rules() -> StructureRules:
    path = Path(__file__).resolve().parents[1] / "examples/chunk-structure-rules.json"
    return StructureRules.model_validate_json(path.read_bytes())


def load_chunk_config(strategy: str) -> ChunkConfig:
    if strategy not in {"structure", "fixed"}:
        raise ValueError("unknown chunk strategy")
    path = Path(__file__).resolve().parents[1] / f"examples/chunk-{strategy}.json"
    return ChunkConfig.model_validate_json(path.read_bytes())


def fingerprint(model: Contract) -> str:
    return content_hash(model.model_dump(mode="json"))

"""Offsets are Python character positions in a fixed extracted page or JSON field."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract
from ingestion.contracts import Digest, SnapshotID


class ParseRequest(Contract):
    snapshot_id: SnapshotID
    ordinal: Annotated[int, Field(strict=True, ge=0)] = 0


class ParseConfig(Contract):
    parser_version: Literal["source-text/0.1"] = "source-text/0.1"
    pdf_extraction_mode: Literal["layout"] = "layout"
    pdf_strict: Literal[True] = True
    layout_mode_space_vertically: Literal[True] = True
    layout_mode_strip_rotated: Literal[False] = False
    paragraph_rule: Literal["blank_line_regions/0.1"] = "blank_line_regions/0.1"
    max_pages: Annotated[int, Field(strict=True, ge=1, le=500)] = 200
    max_text_characters: Annotated[int, Field(strict=True, ge=1, le=5_000_000)] = 2_000_000


class Citation(Contract):
    schema_version: Literal["text-citation/0.1"] = "text-citation/0.1"
    parse_artifact_sha256: Digest
    source_version_id: Digest
    raw_sha256: Digest
    parser_sha256: Digest
    unit_id: Digest
    text_sha256: Digest
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, gt=0)]
    quote: Annotated[str, Field(min_length=1)]
    quote_sha256: Digest

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("citation end must follow start")
        return self

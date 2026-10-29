"""Replay source bytes and the exact parser recipe before accepting a citation."""

from pathlib import Path

import pypdf

from ingestion.records import content_hash
from parsing.contracts import Citation, ParseConfig, ParseRequest
from parsing.parser import parse_source, text_hash
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def checked_parse(raw_root: Path, parsed_root: Path, digest: str) -> dict:
    saved = ArtifactStore(parsed_root).read_json(digest)
    if saved.get("schema_version") != "parsed-source/0.1":
        raise ValueError("unsupported parsed source schema")
    recipe = saved["parser"]
    if content_hash(recipe) != saved["parser_sha256"]:
        raise ArtifactIntegrityError("parser fingerprint differs")
    if recipe["library"] == "pypdf" and recipe["library_version"] != pypdf.__version__:
        raise ValueError("citation replay requires the recorded pypdf version")
    request = ParseRequest(
        snapshot_id=saved["source"]["snapshot_id"], ordinal=saved["source"]["ordinal"]
    )
    replayed = parse_source(raw_root, request, ParseConfig.model_validate(recipe["config"]))
    if replayed != saved:
        raise ArtifactIntegrityError("replayed source text differs from the stored parse")
    return saved


def make_citation(
    raw_root: Path, parsed_root: Path, digest: str, unit_index: int, start: int, end: int
) -> Citation:
    parsed = checked_parse(raw_root, parsed_root, digest)
    if parsed["status"] != "parsed":
        raise ValueError("incomplete parsing requires review before citation")
    if not 0 <= unit_index < len(parsed["units"]):
        raise ValueError("unit position is outside the parsed document")
    unit = parsed["units"][unit_index]
    if not 0 <= start < end <= len(unit["text"]):
        raise ValueError("citation offsets are outside the text unit")
    quote = unit["text"][start:end]
    if not quote.strip():
        raise ValueError("citation cannot contain only whitespace")
    return Citation(
        parse_artifact_sha256=digest,
        source_version_id=parsed["source"]["source_version_id"],
        raw_sha256=parsed["source"]["raw_sha256"],
        parser_sha256=parsed["parser_sha256"],
        unit_id=unit["unit_id"],
        text_sha256=unit["text_sha256"],
        start=start,
        end=end,
        quote=quote,
        quote_sha256=text_hash(quote),
    )


def resolve_citation(raw_root: Path, parsed_root: Path, citation: Citation) -> dict:
    parsed = checked_parse(raw_root, parsed_root, citation.parse_artifact_sha256)
    if parsed["status"] != "parsed":
        raise ValueError("incomplete parsing requires review before citation")
    source = parsed["source"]
    if (citation.source_version_id, citation.raw_sha256, citation.parser_sha256) != (
        source["source_version_id"],
        source["raw_sha256"],
        parsed["parser_sha256"],
    ):
        raise ArtifactIntegrityError("citation source or parser identity differs")
    units = [unit for unit in parsed["units"] if unit["unit_id"] == citation.unit_id]
    if len(units) != 1:
        raise ArtifactIntegrityError("citation unit is missing or ambiguous")
    unit = units[0]
    if not 0 <= citation.start < citation.end <= len(unit["text"]):
        raise ArtifactIntegrityError("citation range is outside its unit")
    actual = unit["text"][citation.start : citation.end]
    if (
        unit["text_sha256"] != citation.text_sha256
        or actual != citation.quote
        or text_hash(actual) != citation.quote_sha256
    ):
        raise ArtifactIntegrityError("citation quote or text fingerprint differs")
    return {
        "status": "verified",
        "source": source,
        "page_number": unit["page_number"],
        "field_pointer": unit["field_pointer"],
        "unit_id": unit["unit_id"],
        "start": citation.start,
        "end": citation.end,
        "quote_sha256": citation.quote_sha256,
        "overlapping_regions": [
            r["region_id"]
            for r in unit["regions"]
            if r["start"] < citation.end and citation.start < r["end"]
        ],
        "verification": "byte_integrity_and_exact_text_replay_not_semantic_support",
    }

"""Preserve page/field text without silently turning layout regions into semantic claims."""

import hashlib
import re
from io import BytesIO
from pathlib import Path

import pypdf
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from ingestion.records import content_hash, prepare_import
from ingestion.snapshots import SnapshotStore
from parsing.contracts import ParseConfig, ParseRequest


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_record(root: Path, request: ParseRequest) -> tuple[dict, dict]:
    batch = prepare_import(root, request.snapshot_id)
    if request.ordinal >= len(batch.rows):
        raise ValueError("source row position is outside the snapshot")
    row = batch.rows[request.ordinal]
    if row.version is None:
        raise ValueError("rejected source row cannot be parsed")
    snapshot = batch.snapshot
    assert snapshot.raw is not None
    source = {
        "snapshot_id": snapshot.snapshot_id,
        "ordinal": request.ordinal,
        "source_id": snapshot.source.source_id,
        "source_kind": snapshot.source.kind,
        "source_url": snapshot.source.url,
        "source_version_id": row.version["version_id"],
        "source_content_sha256": row.version["content_sha256"],
        "mapping_version": row.version["mapping_version"],
        "raw_sha256": snapshot.raw.sha256,
        "raw_object_path": f"objects/{snapshot.raw.sha256}",
        "snapshot_manifest_path": f"snapshots/{snapshot.snapshot_id}.json",
        "row_pointer": row.pointer,
    }
    return source, row.version


def make_unit(text: str, *, page: int | None, field_pointer: str | None) -> dict:
    unit = {
        "kind": "pdf_page" if page is not None else "json_field",
        "page_number": page,
        "field_pointer": field_pointer,
        "text": text,
        "text_sha256": text_hash(text),
        "offset_basis": "decoded_python_unicode_codepoints_half_open",
        "reading_order": "unverified_layout" if page is not None else "source_field_order",
        "table_semantics": "not_inferred" if page is not None else "not_applicable",
    }
    unit_id = content_hash(unit)
    # JSON narrative fields stay whole. PDF regions are only blank-line candidates.
    spans = []
    if page is None:
        spans = [(0, len(text))] if text else []
    else:
        cursor = 0
        for boundary in re.finditer(r"\n[ \t]*\n", text):
            spans.append((cursor, boundary.start()))
            cursor = boundary.end()
        spans.append((cursor, len(text)))
    regions: list[dict] = []
    for start, end in spans:
        segment = text[start:end]
        if not segment.strip():
            continue
        if page is not None:
            start += len(segment) - len(segment.lstrip())
            end -= len(segment) - len(segment.rstrip())
        region = {
            "parent_unit_id": unit_id,
            "ordinal": len(regions),
            "start": start,
            "end": end,
            "text_sha256": text_hash(text[start:end]),
            "kind": "layout_region_candidate" if page is not None else "whole_narrative_field",
        }
        regions.append(region | {"region_id": content_hash(region)})
    return unit | {"unit_id": unit_id, "regions": regions}


def parse_source(root: Path, request: ParseRequest, config: ParseConfig | None = None) -> dict:
    config = config or ParseConfig()
    source, version = source_record(root, request)
    recipe = {
        "config": config.model_dump(mode="json"),
        "library": "pypdf" if source["source_kind"] == "investigation_pdf" else "stdlib_json",
        "library_version": pypdf.__version__
        if source["source_kind"] == "investigation_pdf"
        else None,
        "ocr_engine": None,
        "text_transform": "none",
        "json_fields": ["summary", "Summary", "Consequence", "Remedy", "Notes"],
    }
    units: list[dict] = []
    issues: list[dict] = []
    total_pages = None
    if source["source_kind"] == "investigation_pdf":
        snapshot = SnapshotStore(root).verify(request.snapshot_id)
        assert snapshot.raw is not None
        body = SnapshotStore(root).raw_bytes(snapshot.raw)
        try:
            reader = PdfReader(BytesIO(body), strict=config.pdf_strict)
            if reader.is_encrypted:
                issues.append({"page_number": None, "code": "encrypted_pdf_not_supported"})
            else:
                total_pages = len(reader.pages)
                if not 0 < total_pages <= config.max_pages:
                    issues.append({"page_number": None, "code": "page_limit_or_empty_document"})
                else:
                    characters = 0
                    for number, page in enumerate(reader.pages, 1):
                        try:
                            text = page.extract_text(
                                extraction_mode=config.pdf_extraction_mode,
                                layout_mode_space_vertically=config.layout_mode_space_vertically,
                                layout_mode_strip_rotated=config.layout_mode_strip_rotated,
                            )
                        except (PyPdfError, ValueError, TypeError, KeyError, NotImplementedError):
                            issues.append({"page_number": number, "code": "page_extraction_failed"})
                            continue
                        if not text.strip():
                            issues.append(
                                {
                                    "page_number": number,
                                    "code": "no_extractable_text_needs_review_or_ocr",
                                }
                            )
                            continue
                        characters += len(text)
                        if characters > config.max_text_characters:
                            units = []
                            issues.append({"page_number": number, "code": "text_limit_exceeded"})
                            break
                        units.append(make_unit(text, page=number, field_pointer=None))
        except (PyPdfError, ValueError, TypeError, KeyError, NotImplementedError):
            units = []
            issues.append({"page_number": None, "code": "pdf_parse_failed"})
    else:
        fields = (
            ["summary"]
            if source["source_kind"] == "complaint_api"
            else ["Summary", "Consequence", "Remedy", "Notes"]
        )
        for field in fields:
            text = version["payload"].get(field)
            if isinstance(text, str) and text.strip():
                units.append(
                    make_unit(text, page=None, field_pointer=f"{source['row_pointer']}/{field}")
                )
        if sum(len(u["text"]) for u in units) > config.max_text_characters:
            units = []
            issues.append({"page_number": None, "code": "text_limit_exceeded"})
        elif not units:
            issues.append({"page_number": None, "code": "no_narrative_fields"})
    if not issues and units:
        status = "parsed"
    elif units:
        status = "partial"
    elif issues and all(i["code"] == "no_extractable_text_needs_review_or_ocr" for i in issues):
        status = "needs_review_or_ocr"
    else:
        status = "failed"
    return {
        "schema_version": "parsed-source/0.1",
        "source": source,
        "parser": recipe,
        "parser_sha256": content_hash(recipe),
        "status": status,
        "total_pdf_pages": total_pages,
        "units": units,
        "issues": issues,
        "text_characters": sum(len(u["text"]) for u in units),
        "ocr_performed": False,
        "layout_review": "not_performed_by_parser",
        "semantic_chunks": "not_built",
        "dataset_release": None,
    }

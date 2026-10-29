"""Deterministic row mapping; no entity resolution or historical visibility claims."""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ingestion.contracts import Snapshot
from ingestion.snapshots import SnapshotStore

MAPPING_VERSION = "source-record/0.1"


def content_hash(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def date_field(raw: object, *, format_verified: bool) -> dict:
    result = {"raw": raw, "value": None, "status": "missing", "basis": None}
    if raw is None or raw == "":
        return result
    if not format_verified:
        return result | {"status": "format_unverified"}
    result.update(status="invalid", basis="observed_api_MM/DD/YYYY_v0.1")
    if isinstance(raw, str) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
        try:
            value = datetime.strptime(raw, "%m/%d/%Y").date().isoformat()
        except ValueError:
            pass
        else:
            result.update(value=value, status="parsed")
    return result


class RowRejected(ValueError):
    """Machine-readable code only, never include complaint text or a VIN."""


def text(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RowRejected("required_text_missing")
    return value


def map_row(snapshot: Snapshot, row: dict) -> tuple[str, dict]:
    dates = {
        "event_date": date_field(None, format_verified=False),
        "filed_date": date_field(None, format_verified=False),
        "report_received_date": date_field(None, format_verified=False),
    }
    if snapshot.source.kind == "complaint_api":
        identifier = row.get("odiNumber")
        if type(identifier) is not int or identifier <= 0:
            raise RowRejected("odi_number_invalid")
        text(row, "summary")
        products = row.get("products")
        if (
            not isinstance(products, list)
            or not products
            or any(not isinstance(p, dict) for p in products)
        ):
            raise RowRejected("products_invalid")
        selection = snapshot.source.selection
        if not any(
            p.get("productMake") == selection["make"]
            and p.get("productModel") == selection["model"]
            and p.get("productYear") == str(selection["model_year"])
            for p in products
        ):
            raise RowRejected("row_outside_selection")
        record_key = str(identifier)
        dates["event_date"] = date_field(row.get("dateOfIncident"), format_verified=True)
        dates["filed_date"] = date_field(row.get("dateComplaintFiled"), format_verified=True)
    else:
        campaign = text(row, "NHTSACampaignNumber")
        if (
            not re.fullmatch(r"\d{2}V\d{6}", campaign)
            or campaign != snapshot.source.selection["campaign_number"]
        ):
            raise RowRejected("campaign_invalid_or_outside_selection")
        # This endpoint supplies no stable row ID: preserve the exact vehicle/component tuple.
        # Changing a tuple member makes a new key, not an inferred entity match.
        record_key = json.dumps(
            [campaign, *[text(row, key) for key in ("Make", "Model", "ModelYear", "Component")]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        text(row, "Summary")
        dates["report_received_date"] = date_field(
            row.get("ReportReceivedDate"), format_verified=False
        )
    return record_key, {"dates": dates, "version_public_at": None}


@dataclass(frozen=True)
class MappedRow:
    ordinal: int
    pointer: str
    version: dict | None
    error_code: str | None


@dataclass(frozen=True)
class ImportBatch:
    snapshot: Snapshot
    rows: list[MappedRow]
    report: dict


def prepare_import(root: Path, snapshot_id: str) -> ImportBatch:
    store = SnapshotStore(root)
    snapshot = store.verify(snapshot_id)
    if snapshot.status != "captured" or snapshot.raw is None:
        raise ValueError("only captured snapshots can be imported")
    body = store.raw_bytes(snapshot.raw)
    document = snapshot.source.kind == "investigation_pdf"
    input_rows = [{}] if document else json.loads(body)["results"]
    mapped = []
    for ordinal, row in enumerate(input_rows):
        pointer = "$document" if document else f"/results/{ordinal}"
        try:
            if document:
                record_key = snapshot.source.url
                normalized = {"text_parse_status": "not_started", "version_public_at": None}
                payload = {"raw_sha256": snapshot.raw.sha256, "byte_size": snapshot.raw.byte_size}
            else:
                record_key, normalized = map_row(snapshot, row)
                payload = row
            digest = content_hash(payload)
            version_id = content_hash([snapshot.source.kind, record_key, digest, MAPPING_VERSION])
            version = {
                "version_id": version_id,
                "source_kind": snapshot.source.kind,
                "record_key": record_key,
                "content_sha256": digest,
                "mapping_version": MAPPING_VERSION,
                "payload": payload,
                "normalized": normalized,
            }
            mapped.append(MappedRow(ordinal, pointer, version, None))
        except RowRejected as error:
            mapped.append(MappedRow(ordinal, pointer, None, str(error)))
    accepted = [item.version for item in mapped if item.version is not None]
    date_issues = Counter(
        field["status"]
        for v in accepted
        for field in v["normalized"].get("dates", {}).values()
        if field["status"] in {"invalid", "format_unverified"}
    )
    report = {
        "batch_sha256": content_hash(
            [
                {
                    "ordinal": item.ordinal,
                    "pointer": item.pointer,
                    "version": item.version,
                    "error_code": item.error_code,
                }
                for item in mapped
            ]
        ),
        "snapshot_id": snapshot_id,
        "source_kind": snapshot.source.kind,
        "mapping_version": MAPPING_VERSION,
        "container_record_count": snapshot.record_count,
        "input_units": len(mapped),
        "accepted": len(accepted),
        "rejected": len(mapped) - len(accepted),
        "distinct_record_keys": len({v["record_key"] for v in accepted}),
        "distinct_versions": len({v["version_id"] for v in accepted}),
        "date_issues": dict(date_issues),
        "errors": dict(Counter(item.error_code for item in mapped if item.error_code)),
        "pdf_text_parsed": False,
        "usage": snapshot.source.usage,
    }
    return ImportBatch(snapshot, mapped, report)

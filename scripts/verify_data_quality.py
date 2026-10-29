"""Reproduce source quality and availability observations from archived bytes."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from importlib.metadata import version as package_version
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from ingestion.records import prepare_import
from ingestion.temporal import TemporalRequest, select_versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index", type=Path, default=Path("docs/data/raw-snapshot-observations-2026-10-26.json")
    )
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--output", type=Path, default=Path(".local/data-quality.json"))
    args = parser.parse_args()
    index = json.loads(args.index.read_text(encoding="utf-8"))
    batches = [prepare_import(args.root, item["snapshot_id"]) for item in index["snapshots"]]
    assert [b.snapshot.model_dump(mode="json") for b in batches] == index["snapshots"]
    versions = {
        row.version["version_id"]: row.version
        for batch in batches
        for row in batch.rows
        if row.version
    }
    date_statuses: dict[str, Counter] = defaultdict(Counter)
    event_after_filed = 0
    for version in versions.values():
        fields = version["normalized"].get("dates", {})
        for name, field in fields.items():
            date_statuses[name][field["status"]] += 1
        event, filed = fields.get("event_date", {}), fields.get("filed_date", {})
        if event.get("status") == filed.get("status") == "parsed":
            event_after_filed += int(
                date.fromisoformat(event["value"]) > date.fromisoformat(filed["value"])
            )
    availability = []
    seen = set()
    for batch in batches:
        snapshot = batch.snapshot
        if snapshot.source.source_id in seen:
            continue
        seen.add(snapshot.source.source_id)
        entry = {
            "source_id": snapshot.source.source_id,
            "kind": snapshot.source.kind,
            "snapshot_id": snapshot.snapshot_id,
            "sha256": snapshot.raw.sha256,
            "downloaded": True,
            "version_locatable": True,
            "additional_to_structured_records": None,
        }
        if snapshot.source.kind == "investigation_pdf":
            raw = (args.root / "objects" / snapshot.raw.sha256).read_bytes()
            reader = PdfReader(BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
            checks = {
                "action_heading": "Action:" in pages[0],
                "summary_heading": "Summary:" in pages[0],
                "investigation_id": "Investigation: PE 10-006" in pages[0],
            }
            assert all(checks.values())
            entry.update(
                parser="pypdf",
                parse_status="text_extracted",
                page_count=len(pages),
                page_text_chars=[len(text) for text in pages],
                page_text_sha256=[
                    hashlib.sha256(text.encode("utf-8")).hexdigest() for text in pages
                ],
                verified_locators=["page=1; Action", "page=1; Summary"],
                locator_checks=checks,
                additional_to_structured_records=True,
                additional_information_basis="human_review_recorded_in_availability_matrix",
                formal_chunk_parser=False,
            )
        else:
            entry.update(
                parse_status="json_rows_mapped",
                container_record_count=snapshot.record_count,
                mapped_rows=batch.report["accepted"],
                rejected_rows=batch.report["rejected"],
                verified_locators=["/results/<ordinal>"],
                formal_chunk_parser=False,
            )
        availability.append(entry)
    selections = {}
    for name in ["time-snapshot", "time-historical-2014", "time-historical-observed"]:
        request = TemporalRequest.model_validate_json(Path(f"examples/{name}.json").read_bytes())
        result = select_versions(args.root, request)
        selections[name] = {
            key: value for key, value in result.items() if key not in {"selected", "excluded"}
        }
    assert selections["time-snapshot"]["analysis_label"] == "fixed_snapshot_analysis"
    assert selections["time-historical-2014"]["status"] == "unsupported"
    assert selections["time-historical-observed"]["selected_count"] == len(versions)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "local_verification_of_real_archived_source_bytes_no_model_experiment",
        "snapshot_count": len(batches),
        "distinct_raw_objects": len({b.snapshot.raw.sha256 for b in batches}),
        "accepted_input_units": sum(b.report["accepted"] for b in batches),
        "rejected_input_units": sum(b.report["rejected"] for b in batches),
        "distinct_versions": len(versions),
        "versions_by_kind": dict(Counter(v["source_kind"] for v in versions.values())),
        "date_statuses_by_distinct_version": {
            key: dict(value) for key, value in date_statuses.items()
        },
        "event_after_filed_count": event_after_filed,
        "known_version_publication_times": sum(
            v["normalized"]["version_public_at"] is not None for v in versions.values()
        ),
        "availability": availability,
        "temporal_selections": selections,
        "new_https_requests": 0,
        "pdf_library_version": package_version("pypdf"),
        "external_model_calls": 0,
        "population_completeness": "not_established",
        "usage": "exploration_only",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "distinct_versions": len(versions),
                "rejected": record["rejected_input_units"],
                "event_after_filed_count": event_after_filed,
                "pdf_pages": [
                    a["page_count"] for a in availability if a["kind"] == "investigation_pdf"
                ],
                "temporal_results": {
                    key: {k: r[k] for k in ("status", "selected_count", "excluded_count")}
                    for key, r in selections.items()
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

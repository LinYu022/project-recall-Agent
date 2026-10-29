"""Version visibility over an explicitly frozen set of raw snapshot receipts."""

from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, model_validator

from domain.tasks import Contract, TimeScope
from ingestion.contracts import SnapshotID
from ingestion.records import MAPPING_VERSION, ImportBatch, content_hash, prepare_import


def raw_sha(batch: ImportBatch) -> str:
    assert batch.snapshot.raw is not None
    return batch.snapshot.raw.sha256


class TemporalRequest(Contract):
    snapshot_ids: Annotated[list[SnapshotID], Field(min_length=1)]
    time: TimeScope

    @model_validator(mode="after")
    def unique_snapshots(self) -> Self:
        if len(set(self.snapshot_ids)) != len(self.snapshot_ids):
            raise ValueError("snapshot_ids must be unique")
        return self


def select_versions(root: Path, request: TemporalRequest) -> dict:
    batches = [prepare_import(root, identifier) for identifier in sorted(request.snapshot_ids)]
    historical = request.time.mode == "historical_as_of"
    cutoff = request.time.knowledge_cutoff
    if not historical:
        source_bodies: dict[str, set[str]] = defaultdict(set)
        for batch in batches:
            source_bodies[batch.snapshot.source.source_id].add(raw_sha(batch))
        if any(len(bodies) > 1 for bodies in source_bodies.values()):
            raise ValueError("snapshot_analysis requires one body per source selection")
    versions: dict[str, dict] = {}
    references: dict[str, list[dict]] = defaultdict(list)
    rejected_rows = sum(batch.report["rejected"] for batch in batches)
    eligible_snapshots: list[str] = []
    unavailable_snapshots: list[str] = []
    for batch in batches:
        snapshot = batch.snapshot
        assert snapshot.fetched_at is not None
        visible = not historical or (
            cutoff is not None and snapshot.fetched_at is not None and snapshot.fetched_at <= cutoff
        )
        (eligible_snapshots if visible else unavailable_snapshots).append(snapshot.snapshot_id)
        for row in batch.rows:
            if row.version is None:
                continue
            version = row.version
            version_id = version["version_id"]
            versions[version_id] = version
            if visible:
                references[version_id].append(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "raw_sha256": raw_sha(batch),
                        "raw_pointer": row.pointer,
                        "observed_at": snapshot.fetched_at.isoformat(),
                        "basis": "local_public_source_fetch",
                    }
                )
    # Visibility precedes event filtering: a later revision must not supply an earlier event date.
    visible_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for version_id, version in versions.items():
        if references[version_id]:
            visible_keys[(version["source_kind"], version["record_key"])].add(version_id)
    conflicts = {
        version_id for group in visible_keys.values() if len(group) > 1 for version_id in group
    }
    selected, excluded = [], []
    interval = request.time.event_interval
    for version_id in sorted(versions):
        version = versions[version_id]
        reason = None
        if not references[version_id]:
            reason = "version_not_observed_by_cutoff"
        elif historical and version_id in conflicts:
            reason = "conflicting_visible_versions"
        elif interval is not None:
            event = version["normalized"].get("dates", {}).get("event_date", {})
            if event.get("status") != "parsed" or event.get("value") is None:
                reason = "event_date_unavailable"
            elif not interval.start <= date.fromisoformat(event["value"]) <= interval.end:
                reason = "event_outside_interval"
        if reason:
            excluded.append({"version_id": version_id, "reason": reason})
        else:
            selected.append(
                {
                    "version_id": version_id,
                    "source_kind": version["source_kind"],
                    "content_sha256": version["content_sha256"],
                    "references": references[version_id],
                    "first_observed_in_input_set": min(
                        references[version_id],
                        key=lambda ref: datetime.fromisoformat(ref["observed_at"]),
                    )["observed_at"],
                }
            )
    uncertain = (
        rejected_rows
        or bool(conflicts)
        or any(item["reason"] != "event_outside_interval" for item in excluded)
    )
    if historical and not eligible_snapshots:
        status = "unsupported"
    elif selected:
        status = "partial" if uncertain else "selected"
    else:
        status = "unsupported" if uncertain else "empty"
    inputs = [
        {
            "snapshot_id": batch.snapshot.snapshot_id,
            "raw_sha256": raw_sha(batch),
            "batch_sha256": batch.report["batch_sha256"],
        }
        for batch in batches
    ]
    result = {
        "schema_version": "temporal-selection/0.1",
        "purpose": "raw_snapshot_inspection",
        "request": request.model_dump(mode="json"),
        "mapping_version": MAPPING_VERSION,
        "input_set_sha256": content_hash(inputs),
        "inputs": inputs,
        "status": status,
        "selected": selected,
        "excluded": excluded,
        "selected_count": len(selected),
        "excluded_count": len(excluded),
        "exclusion_reasons": dict(Counter(item["reason"] for item in excluded)),
        "rejected_input_rows": rejected_rows,
        "eligible_snapshot_ids": eligible_snapshots,
        "snapshots_observed_after_cutoff": unavailable_snapshots,
        "visible_version_conflicts": len(conflicts),
        "analysis_label": "bounded_observed_versions_as_of"
        if historical
        else "fixed_snapshot_analysis",
        "event_filter_field": "event_date" if interval is not None else None,
        "coverage": "selected_local_receipts_only",
        "dataset_release": None,
        "source_publication_times_known": False,
    }
    return result | {"selection_sha256": content_hash(result)}

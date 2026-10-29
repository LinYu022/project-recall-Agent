"""Verified frozen scope and exhaustive structured lookup, independent of retrieval top-k."""

from ingestion.records import prepare_import
from ingestion.temporal import TemporalRequest, select_versions
from releases.bundle import read_json
from retrieval.contracts import SearchScope
from retrieval.index import checked_index
from retrieval.scope import classify_scope


class FrozenContext:
    def __init__(self, service, task_id, expected=None):
        self.service = service
        with service.lock, service.catalog.transaction(shared=True) as connection:
            self.task, self.manifest = service.catalog.task(connection, task_id, expected)
            self.base = service.catalog.store.directory(self.manifest)
            self.corpus = checked_index(
                self.base / "index", self.manifest["index_artifact_sha256"]
            )["corpus"]
            self.scope = SearchScope.model_validate(self.task["spec"]["scope"])
            self.sources = self.task["spec"]["allowed_source_ids"]
            self.time = self.corpus["recipe"]["request"]["time"]
            snapshots = [
                sid
                for sid in self.manifest["snapshot_ids"]
                if read_json(self.base / f"raw/snapshots/{sid}.json")["source"]["source_id"]
                in self.sources
            ]
            selection = select_versions(
                self.base / "raw", TemporalRequest(snapshot_ids=snapshots, time=self.time)
            )
            if selection["status"] not in {"selected", "empty"}:
                raise ValueError("incomplete temporal selection")
            selected = {v["version_id"] for v in selection["selected"]}
            self.records = {}
            self.source_kinds = set()
            for sid in snapshots:
                batch = prepare_import(self.base / "raw", sid)
                self.source_kinds.add(batch.snapshot.source.kind)
                for row in batch.rows:
                    if row.version and row.version["version_id"] in selected:
                        self.records[row.version["version_id"]] = row.version
            self.documents = {d["document_id"]: d for d in self.corpus["documents"]}
            self.metadata = {
                d["metadata"]["source_version_id"]: d["metadata"] for d in self.corpus["documents"]
            }

    def lookup(self):
        rows = []
        for version, record in sorted(self.records.items()):
            if record["source_kind"] != "recall_api":
                continue
            metadata = self.metadata.get(version)
            channel, reasons = classify_scope(metadata or {}, self.scope)
            if metadata is None:
                channel, reasons = "pending", ["record_metadata_unavailable"]
            rows.append(
                {
                    "source_version_id": version,
                    "channel": channel,
                    "reasons": reasons,
                    "campaign_number": record["payload"].get("NHTSACampaignNumber"),
                }
            )
        counts = {
            c: sum(r["channel"] == c for r in rows) for c in ("matched", "pending", "excluded")
        }
        available = "recall_api" in self.source_kinds
        return {
            "task_id": self.task["task_id"],
            "release_id": self.manifest["release_id"],
            "status": "unsupported"
            if not available
            else "partial"
            if counts["pending"]
            else "complete",
            "coverage": "all_selected_versions_in_allowed_frozen_sources_only",
            "time": self.time,
            "scope": self.scope.model_dump(mode="json"),
            "source_ids": self.sources,
            "rows": rows,
            "counts": counts,
            "distinct_matched_campaigns": len(
                {
                    r["campaign_number"]
                    for r in rows
                    if r["channel"] == "matched" and r["campaign_number"]
                }
            ),
            "zero_interpretation": "does_not_establish_no_recalls_in_the_world",
        }

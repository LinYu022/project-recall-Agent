"""Raw snapshots record observations, without inventing publication times."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from domain.tasks import Contract

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SnapshotID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
SnapshotStatus = Literal[
    "captured", "http_error", "transport_error", "too_large", "invalid_payload"
]


class SourceSpec(Contract):
    source_id: Annotated[str, Field(pattern=r"^[A-Z0-9-]+$")]
    kind: Literal["recall_api", "complaint_api", "investigation_pdf"]
    url: str
    selection: dict[str, str | int]
    max_bytes: Annotated[int, Field(strict=True, gt=0, le=8_388_608)] = 2_097_152
    usage: Literal["exploration_only", "development_only", "evaluation_only"] = "exploration_only"


class RawObject(Contract):
    sha256: Digest
    byte_size: Annotated[int, Field(strict=True, ge=0)]


class Snapshot(Contract):
    schema_version: Literal["raw-snapshot/0.1"] = "raw-snapshot/0.1"
    snapshot_id: SnapshotID
    source: SourceSpec
    request_started_at: AwareDatetime
    completed_at: AwareDatetime
    fetched_at: AwareDatetime | None
    status: SnapshotStatus
    http_status: int | None
    http_headers: dict[str, str]
    raw: RawObject | None
    inspector_version: Literal["raw-container/0.1"] = "raw-container/0.1"
    record_count: Annotated[int, Field(strict=True, ge=0)] | None
    error_code: str | None
    # No API date field or HTTP cache header proves this particular version was public earlier.
    version_public_at: None = None
    historical_evidence: Literal["local_fetch_only"] = "local_fetch_only"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.completed_at < self.request_started_at:
            raise ValueError("completion precedes request")
        if (self.raw is None) != (self.fetched_at is None):
            raise ValueError("complete body and fetch observation must be recorded together")
        if self.fetched_at is not None and self.fetched_at != self.completed_at:
            raise ValueError("fetch observation is the response completion time")
        if self.status == "captured" and (
            self.http_status != 200 or self.raw is None or self.error_code is not None
        ):
            raise ValueError("captured requires a complete, inspected HTTP 200 body")
        if self.status != "captured" and (self.record_count is not None or self.error_code is None):
            raise ValueError("failures must have an error, never a successful zero count")
        return self

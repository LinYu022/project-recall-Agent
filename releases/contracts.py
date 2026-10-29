from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest, SnapshotID
from retrieval.contracts import Method, SearchScope


class BindTask(Contract):
    task_id: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]+$")]
    release_id: Digest | None = None
    allowed_source_ids: Annotated[list[Text], Field(min_length=1)]
    scope: SearchScope = Field(default_factory=SearchScope)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(self.allowed_source_ids) != len(set(self.allowed_source_ids)):
            raise ValueError("duplicate task source IDs")
        return self


class ReleaseQuery(Contract):
    task_id: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]+$")]
    expected_release_id: Digest | None = None
    query: Annotated[str, Field(min_length=1, max_length=1000)]
    method: Method = "hybrid"
    top_k: Annotated[int, Field(strict=True, ge=1, le=30)] = 10


class PublishRequest(Contract):
    release_id: Digest
    manifest_sha256: Digest
    expected_default: Digest | None
    retain_until: AwareDatetime
    action: Literal["publish", "rollback"] = "publish"


class ReportReference(Contract):
    reference_id: Text
    task_id: Text
    artifact_sha256: Digest
    keep_until: AwareDatetime | None = None


class ReleaseFile(Contract):
    path: Text
    sha256: Digest
    bytes: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def relative_path(self):
        if (
            self.path.startswith("/")
            or "\\" in self.path
            or ":" in self.path
            or any(p in {"..", ".", ""} for p in self.path.split("/"))
        ):
            raise ValueError("release files require clean relative POSIX paths")
        return self


class ReleaseEnvironment(Contract):
    python: Text
    uv_lock_sha256: Digest
    pyproject_sha256: Digest


class ReleaseManifest(Contract):
    schema_version: Literal["dataset-release/0.1"]
    release_id: Digest
    bundle_key: Digest
    dataset_build_id: Digest
    index_artifact_sha256: Digest
    inventory: Annotated[list[ReleaseFile], Field(min_length=1)]
    snapshot_ids: Annotated[list[SnapshotID], Field(min_length=1)]
    record_hashes: Annotated[dict[Digest, Digest], Field(min_length=1)]
    retention_days: Annotated[int, Field(strict=True, ge=1)]
    environment: ReleaseEnvironment
    test_only: Annotated[bool, Field(strict=True)]
    allowed_source_ids: Annotated[list[Text], Field(min_length=1)]
    qdrant_integrity: Literal[
        "logical_vectors_and_payloads_plus_component_manifest_not_mutable_sqlite_bytes"
    ]

    @model_validator(mode="after")
    def unique_members(self):
        for group in ([e.path for e in self.inventory], self.snapshot_ids, self.allowed_source_ids):
            if len(group) != len(set(group)):
                raise ValueError("duplicate release component or source")
        return self

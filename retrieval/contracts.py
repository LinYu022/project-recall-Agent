from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text, TimeScope
from ingestion.contracts import Digest, SnapshotID

Method = Literal["bm25", "dense", "hybrid", "hybrid_rerank"]


class RetrievalConfig(Contract):
    schema_version: Literal["retrieval-config/0.1"] = "retrieval-config/0.1"
    bm25_k1: Annotated[float, Field(gt=0, le=5)] = 1.2
    bm25_b: Annotated[float, Field(ge=0, le=1)] = 0.75
    candidate_k: Annotated[int, Field(strict=True, ge=1, le=200)] = 30
    rrf_constant: Annotated[int, Field(strict=True, ge=1)] = 60
    rerank_limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 30


class BuildRequest(Contract):
    schema_version: Literal["retrieval-build-request/0.1"] = "retrieval-build-request/0.1"
    snapshot_ids: Annotated[list[SnapshotID], Field(min_length=1)]
    time: TimeScope
    chunk_strategy: Literal["structure", "fixed"] = "structure"

    @model_validator(mode="after")
    def unique_snapshots(self):
        if len(set(self.snapshot_ids)) != len(self.snapshot_ids):
            raise ValueError("duplicate snapshot IDs")
        return self


class SearchScope(Contract):
    make: Text | None = None
    model: Text | None = None
    model_year: Annotated[int, Field(strict=True, ge=1886, le=2100)] | None = None
    campaign_number: Annotated[str, Field(pattern=r"^\d{2}V\d{6}$")] | None = None
    component: Text | None = None
    production_date: date | None = None
    configurations: dict[Text, Text] = Field(default_factory=dict)


class SearchRequest(Contract):
    schema_version: Literal["retrieval-request/0.1"] = "retrieval-request/0.1"
    dataset_build_id: Digest
    query: Annotated[str, Field(min_length=1, max_length=1000)]
    allowed_source_ids: Annotated[list[Text], Field(min_length=1)]
    scope: SearchScope = Field(default_factory=SearchScope)
    method: Method = "hybrid"
    top_k: Annotated[int, Field(strict=True, ge=1, le=30)] = 10

    @model_validator(mode="after")
    def meaningful_query(self):
        if not self.query.strip() or len(set(self.allowed_source_ids)) != len(
            self.allowed_source_ids
        ):
            raise ValueError("nonblank query and unique source allowlist required")
        return self

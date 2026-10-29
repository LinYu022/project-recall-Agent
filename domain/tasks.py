"""The exploration task contract; this is not an execution authorization."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TextList = Annotated[list[Text], Field(min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventInterval(Contract):
    """Inclusive dates; these are incident dates, not filing dates."""

    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("event_interval.end must not precede start")
        return self


class TimeScope(Contract):
    mode: Literal["snapshot_analysis", "historical_as_of"]
    event_interval: EventInterval | None
    knowledge_cutoff: AwareDatetime | None
    date_filter_basis: Text

    @model_validator(mode="after")
    def cutoff_matches_mode(self) -> Self:
        if self.mode == "historical_as_of" and self.knowledge_cutoff is None:
            raise ValueError("historical_as_of requires knowledge_cutoff")
        if self.mode == "snapshot_analysis" and self.knowledge_cutoff is not None:
            raise ValueError("snapshot_analysis must not imply a knowledge cutoff")
        return self


class VehicleScope(Contract):
    country: Literal["US"]
    make: Text
    model: Text
    model_year: Annotated[int, Field(strict=True, ge=1886, le=2100)]
    component_topic: Text
    campaign_number: Annotated[str, Field(pattern=r"^\d{2}V\d{6}$")]


class TaskSpec(Contract):
    schema_version: Literal["exploration-task/0.2"]
    task_id: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]+$")]
    task_kind: Literal["record_lookup", "cross_source", "complaint_review", "complaint_statistics"]
    split: Literal["exploration", "development", "test"]
    event_cluster_id: Text
    status: Literal["draft", "ready"]
    question: Text
    scope: VehicleScope
    time: TimeScope
    release_id: Text | None
    grant_id: Text | None
    revision_id: Text | None
    allowed_source_ids: TextList
    required_outputs: TextList
    forbidden_inferences: TextList
    missing_information_behavior: Annotated[dict[Text, Text], Field(min_length=1)]

    @model_validator(mode="after")
    def required_business_conditions(self) -> Self:
        if self.status == "ready" and self.release_id is None:
            raise ValueError("ready tasks require release_id")
        if self.task_kind == "complaint_statistics" and self.time.event_interval is None:
            raise ValueError("complaint_statistics requires an incident event_interval")
        return self

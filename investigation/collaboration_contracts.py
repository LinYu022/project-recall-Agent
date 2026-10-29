"""Delegation is a narrowed capability description, never a new authorization channel."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from investigation.contracts import Alias
from retrieval.contracts import SearchScope

ToolName = Literal[
    "lookup",
    "search",
    "expand",
    "count",
    "links",
    "read",
    "finish",
    "history_search",
    "history_window",
]


class Allocation(Contract):
    model_calls: Annotated[int, Field(strict=True, ge=1, le=8)]
    tool_calls: Annotated[int, Field(strict=True, ge=1, le=10)]
    prompt_characters: Annotated[int, Field(strict=True, ge=5000)]
    completion_tokens: Annotated[int, Field(strict=True, ge=256)]


class Delegation(Contract):
    schema_version: Literal["investigation-delegation/0.1"] = "investigation-delegation/0.1"
    delegation_id: Literal["DATA", "DOCUMENT"]
    role: Literal["data", "document"]
    parent_task_id: Text
    revision_id: Text
    authorization_id: Text
    epoch: Annotated[int, Field(strict=True, ge=1)]
    release_id: Digest
    manifest_sha256: Digest
    goal: Annotated[str, Field(min_length=1, max_length=2000)]
    scope: SearchScope
    time: dict
    allowed_source_ids: Annotated[list[Text], Field(min_length=1)]
    input_evidence_ids: list[Digest] = []
    allowed_tools: list[ToolName]
    allocation: Allocation
    completion_conditions: Annotated[list[Text], Field(min_length=1)]

    @model_validator(mode="after")
    def role_capabilities(self):
        tools = {
            "data": {
                "lookup",
                "count",
                "links",
                "read",
                "finish",
                "history_search",
                "history_window",
            },
            "document": {
                "lookup",
                "search",
                "expand",
                "read",
                "finish",
                "history_search",
                "history_window",
            },
        }[self.role]
        if (
            self.delegation_id != {"data": "DATA", "document": "DOCUMENT"}[self.role]
            or not set(self.allowed_tools) <= tools
        ):
            raise ValueError("delegation role exceeds its tool capability")
        if not {"lookup", "finish"} <= set(self.allowed_tools) or len(
            set(self.allowed_source_ids)
        ) != len(self.allowed_source_ids):
            raise ValueError("delegation requires guards and unique sources")
        return self


class Assessment(Contract):
    issue: Literal["applicability", "same_defect", "safety_generalization"]
    subject: Annotated[str, Field(pattern=r"^(?:\d{2}V\d{6}|TASK|PINNED-PAIR)$")]
    value: Literal["supported", "contradicted", "unknown"]
    support_ids: list[Alias] = []
    counter_ids: list[Alias] = []
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def grounded_proposal(self):
        if self.value != "unknown" and not self.support_ids + self.counter_ids:
            raise ValueError("assessment proposal requires evidence")
        return self


class BranchResult(Contract):
    schema_version: Literal["delegated-result/0.1"] = "delegated-result/0.1"
    delegation_id: Literal["DATA", "DOCUMENT"]
    status: Literal["completed", "failed", "timeout"]
    disposition: str | None = None
    report_artifact: dict | None = None
    evidence_ids: list[Digest] = []
    counterevidence_ids: list[Digest] = []
    assessments: list[Assessment] = []
    unresolved_fields: list[str] = []
    error_code: str | None = None
    assessment_status_is_not_semantic_verdict: Literal[True] = True


class BatchAction(Contract):
    action: Literal["search", "expand", "count", "links"]
    query: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    document_id: Annotated[str, Field(pattern=r"^D\d{3}$")] | None = None
    include_related: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def independent_tool_shape(self):
        if (self.action == "search") != (self.query is not None) or (self.action == "expand") != (
            self.document_id is not None
        ):
            raise ValueError("batch tool arguments differ")
        if self.action != "expand" and self.include_related is not None:
            raise ValueError("batch related flag only belongs to expand")
        return self

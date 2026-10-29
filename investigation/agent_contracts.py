from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from investigation.collaboration_contracts import Assessment, BatchAction
from investigation.contracts import Alias, AnswerProposal


class Hypothesis(Contract):
    hypothesis_id: Annotated[str, Field(pattern=r"^H\d{3}$")]
    statement: Annotated[str, Field(min_length=1, max_length=500)]
    status: Literal["candidate", "supported", "contradicted", "unknown"]
    support_ids: list[Alias] = []
    counter_ids: list[Alias] = []


class Decision(Contract):
    action: Literal[
        "search",
        "expand",
        "count",
        "links",
        "read",
        "batch",
        "finish",
        "history_search",
        "history_window",
        "apply_skill",
    ]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=6)]
    evidence_gaps: Annotated[list[Text], Field(max_length=8)]
    next_steps: Annotated[list[Text], Field(max_length=8)]
    query: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    document_id: Annotated[str, Field(pattern=r"^D\d{3}$")] | None = None
    include_related: Annotated[bool, Field(strict=True)] | None = None
    artifact_id: Annotated[str, Field(pattern=r"^A\d{3}$")] | None = None
    evidence_alias: Alias | None = None
    offset: Annotated[int, Field(strict=True, ge=0)] = 0
    page_size: Annotated[int, Field(strict=True, ge=1, le=4000)] = 2000
    proposal: AnswerProposal | None = None
    assessments: Annotated[list[Assessment], Field(max_length=3)] = []
    batch: Annotated[list[BatchAction], Field(max_length=2)] = []
    history_query: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    history_episode_id: Text | None = None
    history_event_id: Text | None = None

    @model_validator(mode="after")
    def action_contract(self):
        if (self.action == "history_search") != (self.history_query is not None):
            raise ValueError("history_query_only_belongs_to_history_search")
        if self.action == "history_window":
            if not self.history_episode_id or not self.history_event_id:
                raise ValueError("history_window_requires_registered_anchor")
        elif self.history_episode_id is not None or self.history_event_id is not None:
            raise ValueError("history_anchor_only_belongs_to_history_window")
        if len({h.hypothesis_id for h in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("duplicate hypothesis IDs")
        if (self.action == "search") != (self.query is not None):
            raise ValueError("query only belongs to search")
        if (self.action == "expand") != (self.document_id is not None):
            raise ValueError("document_id only belongs to expand")
        if self.action != "expand" and self.include_related is not None:
            raise ValueError("include_related only belongs to expand")
        if (self.action == "finish") != (self.proposal is not None):
            raise ValueError("finish requires proposal; tools cannot include an answer")
        if (self.action == "batch") != (len(self.batch) == 2):
            raise ValueError("batch requires exactly two independent tool actions")
        if self.action != "batch" and self.batch:
            raise ValueError("batch actions only belong to batch")
        if self.action == "read":
            if (self.artifact_id is None) == (self.evidence_alias is None):
                raise ValueError("read requires exactly one registered artifact or evidence alias")
        elif (
            self.artifact_id is not None
            or self.evidence_alias is not None
            or self.offset != 0
            or self.page_size != 2000
        ):
            raise ValueError("pagination fields only belong to read")
        return self

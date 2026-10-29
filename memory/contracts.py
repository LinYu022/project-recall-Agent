from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from retrieval.contracts import SearchScope

ProjectID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")]
Kind = Literal["preference", "convention", "rule", "source_pointer", "official_fact", "hypothesis"]
POLICIES = {
    "replay_original_before_claim": "使用官方证据前回放原文及必要条件，记忆本身不是证据。",
    "keep_applicability_unknown": "缺少个车生产日期或配置时，不确认个车适用性。",
}


class Selection(Contract):
    project_id: ProjectID
    enabled: Annotated[bool, Field(strict=True)] = True


class ProjectInput(Contract):
    project_id: ProjectID
    name: Annotated[str, Field(min_length=1, max_length=100)]


class SourceInput(Contract):
    project_id: ProjectID | None = None
    origin: Literal["user_statement", "untrusted_text"]
    text: Annotated[str, Field(min_length=1, max_length=20000)]


class SourcePointer(Contract):
    source_id: Text
    source_version_id: Digest | None = None


class Proposal(Contract):
    kind: Kind
    scope: Literal["user", "project"]
    project_id: ProjectID | None = None
    semantic_key: Key
    value: Annotated[str, Field(max_length=1500)] = ""
    release_id: Digest | None = None
    applies_to: SearchScope = Field(default_factory=SearchScope)
    sources: Annotated[list[SourcePointer], Field(max_length=20)] = []
    policy_id: Literal["replay_original_before_claim", "keep_applicability_unknown"] | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def shape(self):
        if (self.scope == "project") != (self.project_id is not None):
            raise ValueError("project_scope_requires_project")
        if self.kind in {"rule", "source_pointer"}:
            if self.scope != "project" or not self.release_id or not self.sources:
                raise ValueError("domain_memory_requires_project_release_sources")
            if self.value:
                raise ValueError("domain_memory_stores_policy_or_pointer_not_fact_text")
        elif not self.value.strip():
            raise ValueError("text_memory_requires_value")
        if (self.kind == "rule") != (self.policy_id is not None):
            raise ValueError("rule_requires_installed_policy")
        if self.kind in {"preference", "convention"} and (self.release_id or self.sources):
            raise ValueError("preferences_are_not_domain_evidence")
        if self.kind == "convention" and self.scope != "project":
            raise ValueError("convention_requires_project")
        return self


class CandidateInput(Contract):
    source_id: Text
    proposal: Proposal


class Review(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    proposal_sha256: Digest
    decision: Literal["approve", "reject"]
    note: Annotated[str, Field(min_length=1, max_length=500)]
    replace_memory_id: Text | None = None


class Change(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    action: Literal["disable", "delete"]
    note: Annotated[str, Field(min_length=1, max_length=500)]


class Edit(CandidateInput):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class Settings(Contract):
    expected_generation: Annotated[int, Field(strict=True, ge=1)]
    enabled: Annotated[bool, Field(strict=True)]
    project_id: ProjectID | None = None


class Search(Contract):
    project_id: ProjectID
    release_id: Digest
    allowed_source_ids: Annotated[list[Text], Field(max_length=50)]
    scope: SearchScope = Field(default_factory=SearchScope)
    query: Annotated[str, Field(max_length=1000)] = ""
    limit: Annotated[int, Field(strict=True, ge=1, le=8)] = 5


class Extract(Contract):
    source_id: Text


def canonical_value(proposal: Proposal):
    if proposal.kind == "rule":
        assert proposal.policy_id is not None
        return POLICIES[proposal.policy_id]
    if proposal.kind == "source_pointer":
        return "官方资料入口；使用前查询当前允许的冻结证据。"
    return proposal.value


def expired(proposal: Proposal, now: datetime):
    return proposal.expires_at is not None and proposal.expires_at <= now

"""Versioned human/agent review records with metric-specific missingness."""

from typing import Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest

Verdict = Literal["supported", "partial", "unsupported", "not_evaluable"]


class Source(Contract):
    name: Text
    content_sha256: Digest


class Citation(Contract):
    claim_id: Text
    evidence_alias: Text
    role: Literal["direct", "required_context", "both", "counterevidence", "unresolved"]
    direct_verdict: Verdict | None = None
    counter_verdict: Verdict | None = None
    evaluated_aspect: Text | None = None
    reason: Text

    @model_validator(mode="after")
    def role_and_verdict(self):
        if self.role in {"direct", "both"}:
            if self.direct_verdict is None or self.evaluated_aspect is None:
                raise ValueError("direct_review_requires_verdict_and_explicit_aspect")
        elif self.role == "counterevidence":
            if (
                self.direct_verdict is not None
                or self.counter_verdict is None
                or self.evaluated_aspect is None
            ):
                raise ValueError("counterevidence_requires_its_own_relevance_judgment")
        elif self.direct_verdict is not None or self.evaluated_aspect is not None:
            raise ValueError("context_or_unresolved_citation_cannot_imply_direct_support")
        if self.role != "counterevidence" and self.counter_verdict is not None:
            raise ValueError("counter_verdict_requires_counterevidence_role")
        return self


class Claim(Contract):
    claim_id: Text
    verdict: Verdict
    scope_verdict: Literal["correct", "mostly_correct", "partial", "incorrect", "not_evaluable"]
    reason: Text


class Assertion(Contract):
    assertion_id: Text
    claim_id: Text
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)
    text: Text
    verdict: Verdict
    reason: Text


class Obligation(Contract):
    reference_claim_id: Text
    verdict: Literal["satisfied", "partial", "missing", "not_evaluable"]
    claim_ids: list[Text] = []
    support_ids: list[Text] = []
    evidence_basis: Literal["frozen_slots", "reviewer_accepted_equivalent"] = "frozen_slots"
    equivalence_reason: Text | None = None
    reason: Text

    @model_validator(mode="after")
    def grounding(self):
        if len(set(self.claim_ids)) != len(self.claim_ids) or len(set(self.support_ids)) != len(
            self.support_ids
        ):
            raise ValueError("duplicate_obligation_claim_or_evidence")
        if self.verdict == "satisfied" and (not self.claim_ids or not self.support_ids):
            raise ValueError("satisfied_obligation_requires_explicit_claim_and_evidence")
        if (
            self.evidence_basis == "reviewer_accepted_equivalent"
            and self.equivalence_reason is None
        ):
            raise ValueError("novel_equivalent_evidence_requires_explicit_review_reason")
        if self.evidence_basis == "frozen_slots" and self.equivalence_reason is not None:
            raise ValueError("frozen_evidence_cannot_imply_unrecorded_equivalence")
        return self


class Relation(Contract):
    relation_type: Literal[
        "scope_overlap",
        "symptom_similar",
        "officially_referenced",
        "excluded_by_scope",
        "applicability_unknown",
    ]
    verdict: Literal["correct", "wrong", "missed", "correctly_unknown", "not_evaluable"]
    reason: Text


class InputVisibility(Contract):
    input_artifact_sha256: Digest
    input_content_sha256: Digest
    check: Literal["exact_input_checked", "not_checked"]
    note: Text


class Review(Contract):
    schema_version: Literal["role-aware-review/0.2"] = "role-aware-review/0.2"
    policy_sha256: Digest
    experiment_id: Digest
    cell_id: Text
    blind_id: Text
    observation_sha256: Digest
    report_sha256: Digest | None
    reference_sha256: Digest
    reviewer_kind: Literal["human", "implementation_agent", "synthetic"]
    reviewer_id: Text
    source_kind: Literal["actual_human_reply", "agent_analysis", "synthetic_fixture"]
    sources: list[Source] = Field(min_length=1)
    transcribed_by: Text
    complete_task_reading: bool = Field(strict=True)
    task_success: bool | None = Field(default=None, strict=True)
    disposition_correct: bool | None = Field(default=None, strict=True)
    dispute_status: Literal["none", "pending", "adjudicated"] = "none"
    citations: list[Citation] = []
    claims: list[Claim] = []
    assertions: list[Assertion] = []
    obligations: list[Obligation] = []
    relations: list[Relation] = []
    assertions_exhaustive: bool = Field(default=False, strict=True)
    input_visibility: InputVisibility | None = None
    notes: Text

    @model_validator(mode="after")
    def provenance_and_duplicates(self):
        expected = {
            "human": "actual_human_reply",
            "implementation_agent": "agent_analysis",
            "synthetic": "synthetic_fixture",
        }
        if self.source_kind != expected[self.reviewer_kind]:
            raise ValueError("reviewer_kind_conflicts_with_judgment_source")
        groups: list[list[tuple[str, str]]] = [
            [(c.claim_id, c.evidence_alias) for c in self.citations],
            [(c.claim_id, "") for c in self.claims],
            [(a.assertion_id, "") for a in self.assertions],
            [(o.reference_claim_id, "") for o in self.obligations],
            [(r.relation_type, "") for r in self.relations],
            [(s.name, "") for s in self.sources],
        ]
        for values in groups:
            if len(values) != len(set(values)):
                raise ValueError("duplicate_review_judgment_or_source")
        return self

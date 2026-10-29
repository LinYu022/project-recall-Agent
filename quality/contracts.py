"""Explicit model provenance and exhaustive, source-bound component judgments."""

from typing import Literal

from pydantic import Field

from domain.tasks import Contract, Text

Verdict = Literal["supported", "partial", "unsupported", "not_evaluable"]


class Atom(Contract):
    atom_id: Text
    quote: Text
    verdict: Verdict
    support_ids: list[Text]
    reason: Text


class Claim(Contract):
    claim_id: Text
    verdict: Verdict
    scope_verdict: Literal["correct", "minor_caveat", "partial", "incorrect", "not_evaluable"]
    atoms: list[Atom] = Field(min_length=1)
    reason: Text


class Citation(Contract):
    claim_id: Text
    evidence_alias: Text
    role: Literal["direct", "required_context", "both", "counterevidence", "unresolved"]
    atom_ids: list[Text]
    aspect_verdict: Verdict | None
    whole_claim_verdict: Verdict | None
    reason: Text


class Obligation(Contract):
    obligation_id: Text
    verdict: Literal["satisfied", "partial", "missing", "not_evaluable"]
    claim_ids: list[Text]
    support_ids: list[Text]
    reason: Text


class Relation(Contract):
    relation_type: Literal[
        "scope_overlap",
        "symptom_similar",
        "officially_referenced",
        "excluded_by_scope",
        "applicability_unknown",
    ]
    verdict: Literal[
        "correct", "wrong", "missed", "correctly_unknown", "not_applicable", "not_evaluable"
    ]
    reason: Text


class Judgment(Contract):
    schema_version: Literal["model-assisted-judgment/0.1"]
    complete_reading: bool = Field(strict=True)
    disposition_correct: bool | None = Field(strict=True)
    core_complete: bool | None = Field(strict=True)
    all_requirements: Literal[
        "satisfied",
        "mostly_satisfied_with_minor_caveat",
        "not_satisfied",
        "not_evaluable",
    ]
    claims: list[Claim]
    citations: list[Citation]
    obligations: list[Obligation]
    relations: list[Relation]
    missing_or_counterevidence: list[Text]
    reason: Text

"""Versioned OR-of-AND evidence coverage and explicitly attributed semantic review."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from parsing.parser import text_hash


class ClaimReview(Contract):
    claim_id: Text
    verdict: Literal["supported", "partial", "overstated", "not_evaluable"]
    reference_claim_ids: list[Text] = []
    scope_correct: bool
    counterevidence_handled: bool
    reason: Text


class NovelEvidenceDecision(Contract):
    reference_claim_id: Text
    evidence_ids: Annotated[list[Digest], Field(min_length=1)]
    accepted: bool
    reason: Text


class SemanticReview(Contract):
    schema_version: Literal["semantic-review/0.1"] = "semantic-review/0.1"
    report_sha256: Digest
    reference_sha256: Digest
    reviewer_kind: Literal["human", "implementation_agent", "synthetic"]
    reviewer_id: Text
    claims: list[ClaimReview]
    disposition_correct: bool
    complete: bool
    novel_evidence_decisions: list[NovelEvidenceDecision] = []
    dispute_status: Literal["none", "pending", "adjudicated"] = "none"
    notes: Text

    @model_validator(mode="after")
    def unique_claims(self):
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("duplicate reviewed claim")
        return self


def matches_slot(card, slot):
    if card["kind"] != slot.kind:
        return False
    if slot.kind != "text":
        return bool(slot.expected_fields) and all(
            card["payload"].get(k) == v for k, v in slot.expected_fields.items()
        )
    for locator in slot.alternatives:
        for citation in card["payload"]["citations"]:
            if (
                citation["source_version_id"] == locator.source_version_id
                and citation["unit_id"] == locator.unit_id
                and citation["start"] <= locator.start
                and locator.end <= citation["end"]
                and text_hash(
                    citation["quote"][
                        locator.start - citation["start"] : locator.end - citation["start"]
                    ]
                )
                == locator.quote_sha256
            ):
                return True
    return False


def score(report: dict, reference: ReferenceCase, review: SemanticReview | None = None):
    digest = content_hash(report)
    labels = content_hash(reference.model_dump(mode="json"))
    cards = {c["evidence_id"]: c for c in report["cards"]}
    claims = (report.get("proposal") or {}).get("claims", [])
    aliases = report["aliases"]
    reference_ids = {c.reference_claim_id for c in reference.required_claims}
    coverage = {}
    all_matched_slots = set()
    for claim in claims:
        used = [cards[aliases[a]] for a in claim["support_ids"] if a in aliases]
        slots = {s.slot_id for s in reference.slots if any(matches_slot(c, s) for c in used)}
        all_matched_slots |= slots
        coverage[claim["claim_id"]] = {
            "matched_slots": sorted(slots),
            "evidence_sets_complete_for": [
                c.reference_claim_id
                for c in reference.required_claims
                if any(set(group) <= slots for group in c.evidence_sets)
            ],
        }
    unknown = sorted(
        {
            i
            for c in claims
            for a in c["support_ids"]
            if (i := aliases.get(a)) in cards
            and not any(matches_slot(cards[i], s) for s in reference.slots)
        }
    )
    result = {
        "schema_version": "investigation-score/0.1",
        "report_content_sha256": digest,
        "reference_sha256": labels,
        "split": reference.split,
        "machine": {
            "execution_completed": report["execution_status"] == "completed",
            "evidence_verified": report.get("evidence_verification", {}).get("status")
            == "verified",
            "claim_constraints_passed": report.get("claim_verification", {}).get("status")
            == "passed",
            "disposition_in_reference": bool(report.get("proposal"))
            and report["proposal"]["disposition"] in reference.acceptable_dispositions,
            "claim_evidence_coverage": coverage,
            "slot_coverage_fraction": len(all_matched_slots) / len(reference.slots)
            if reference.slots
            else None,
        },
        "novel_evidence_pending": unknown,
        "formal_semantic_metrics": None,
        "provisional_semantic_metrics": None,
        "review_status": "pending",
        "reviewer_kind": None,
    }
    if review is None:
        return result
    if review.report_sha256 != digest or review.reference_sha256 != labels:
        raise ValueError("review is not bound to this exact report/reference")
    if {c.claim_id for c in review.claims} != {c["claim_id"] for c in claims}:
        raise ValueError("review must cover exactly the report claims")
    if any(not set(c.reference_claim_ids) <= reference_ids for c in review.claims):
        raise ValueError("review uses undefined reference claim")
    for decision in review.novel_evidence_decisions:
        if (
            decision.reference_claim_id not in reference_ids
            or not set(decision.evidence_ids) <= cards.keys()
        ):
            raise ValueError("novel evidence decision uses undefined claim/card")
    result["reviewer_kind"] = review.reviewer_kind
    result["review_status"] = "disputed" if review.dispute_status == "pending" else "reviewed"
    if (
        report["execution_status"] != "completed"
        or not review.complete
        or review.dispute_status == "pending"
        or any(c.verdict == "not_evaluable" for c in review.claims)
    ):
        return result
    supported = [
        c
        for c in review.claims
        if c.verdict == "supported" and c.scope_correct and c.counterevidence_handled
    ]
    grounded, recalled = [], set()
    for reviewed in supported:
        claim = next(c for c in claims if c["claim_id"] == reviewed.claim_id)
        used_ids = {aliases[a] for a in claim["support_ids"] if a in aliases}
        covered = set(coverage[reviewed.claim_id]["evidence_sets_complete_for"])
        covered |= {
            d.reference_claim_id
            for d in review.novel_evidence_decisions
            if d.accepted and set(d.evidence_ids) <= used_ids
        }
        valid = covered & set(reviewed.reference_claim_ids)
        recalled |= valid
        if valid:
            grounded.append(reviewed.claim_id)
    metrics = {
        "supported_claim_fraction": len(supported) / len(claims) if claims else None,
        "grounded_claim_fraction": len(grounded) / len(claims) if claims else None,
        "required_claim_recall": len(recalled) / len(reference_ids) if reference_ids else None,
        "disposition_correct": review.disposition_correct,
        "scope_correct_fraction": sum(c.scope_correct for c in review.claims) / len(claims)
        if claims
        else None,
        "counterevidence_handled_fraction": sum(c.counterevidence_handled for c in review.claims)
        / len(claims)
        if claims
        else None,
    }
    key = (
        "formal_semantic_metrics"
        if review.reviewer_kind == "human"
        else "provisional_semantic_metrics"
    )
    result[key] = metrics
    return result

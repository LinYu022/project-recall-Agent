"""Exact-bound review imports; implementation-agent drafts never become human judgments."""

import json
from pathlib import Path
from typing import Literal

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from investigation.scoring import SemanticReview, score
from runtime.artifacts import ArtifactStore


class CitationJudgment(Contract):
    claim_id: Text
    evidence_alias: Text
    verdict: Literal["supported", "partial", "unsupported", "not_evaluable"]
    reason: Text


class RelationJudgment(Contract):
    relation_type: Literal[
        "scope_overlap",
        "symptom_similar",
        "officially_referenced",
        "excluded_by_scope",
        "applicability_unknown",
    ]
    verdict: Literal["correct", "wrong", "missed", "correctly_unknown", "not_evaluable"]
    reason: Text


class ReviewEnvelope(Contract):
    schema_version: Literal["evaluation-review/0.1"] = "evaluation-review/0.1"
    blind_id: Text
    observation_sha256: Digest
    reviewer_kind: Literal["human", "implementation_agent", "synthetic"]
    reviewer_id: Text
    complete: bool
    task_success: bool
    disposition_correct: bool
    dispute_status: Literal["none", "pending", "adjudicated"]
    citations: list[CitationJudgment]
    relations: list[RelationJudgment]
    semantic: SemanticReview | None
    notes: Text


def apply_review(observation, value, case, label_root: Path):
    review = ReviewEnvelope.model_validate(value)
    if review.blind_id != observation.get("blind_id") or review.observation_sha256 != content_hash(
        observation
    ):
        raise ValueError("review_bound_to_another_exact_observation")
    if not review.complete or review.dispute_status == "pending":
        return {"status": "pending", "formal_task_success": None, "formal_metrics": None}
    if len({r.relation_type for r in review.relations}) != len(review.relations):
        raise ValueError("duplicate_relation_judgment")
    citation_metrics = None
    if observation.get("report_reference"):
        pointer = observation["report_reference"]
        report = ArtifactStore(Path(pointer["artifact_root"])).read_json(
            pointer["report_artifact"]["sha256"]
        )
        ref = ReferenceCase.model_validate_json(
            (label_root / "references" / (case.case_id + ".json")).read_bytes()
        )
        if content_hash(ref.model_dump(mode="json")) != case.reference_sha256:
            raise ValueError("review_reference_changed")
        if (
            review.semantic is None
            or review.semantic.reviewer_kind != review.reviewer_kind
            or review.semantic.reviewer_id != review.reviewer_id
        ):
            raise ValueError("review_identity_or_semantics_missing")
        judged = score(report, ref, review.semantic)
        pairs = {
            (c["claim_id"], a)
            for c in (report.get("proposal") or {}).get("claims", [])
            for a in c["support_ids"]
        }
        supplied = {(c.claim_id, c.evidence_alias) for c in review.citations}
        if supplied != pairs or len(supplied) != len(review.citations):
            raise ValueError("review_must_cover_every_citation_claim_pair_once")
        if any(c.verdict == "not_evaluable" for c in review.citations) or any(
            r.verdict == "not_evaluable" for r in review.relations
        ):
            return {"status": "pending", "formal_task_success": None, "formal_metrics": None}
        metrics = (
            judged["formal_semantic_metrics"]
            if review.reviewer_kind == "human"
            else judged["provisional_semantic_metrics"]
        )
        if metrics is None:
            return {"status": "pending", "formal_task_success": None, "formal_metrics": None}
        citation_metrics = {
            "supported_citation_pairs": sum(c.verdict == "supported" for c in review.citations),
            "partial_citation_pairs": sum(c.verdict == "partial" for c in review.citations),
            "unsupported_citation_pairs": sum(c.verdict == "unsupported" for c in review.citations),
            "evaluated_citation_pairs": len(review.citations),
            "citation_support_rate": sum(c.verdict == "supported" for c in review.citations)
            / len(review.citations)
            if review.citations
            else None,
            "unsupported_assertion_rate": sum(
                c.verdict != "supported" or not c.scope_correct or not c.counterevidence_handled
                for c in review.semantic.claims
            )
            / len(review.semantic.claims)
            if review.semantic.claims
            else None,
            "claim_count": len(review.semantic.claims),
            "unsupported_assertion_count": sum(
                c.verdict != "supported" or not c.scope_correct or not c.counterevidence_handled
                for c in review.semantic.claims
            ),
            "partial_assertion_count": sum(c.verdict == "partial" for c in review.semantic.claims),
            "fully_supported_claim_count": sum(
                c.verdict == "supported" and c.scope_correct and c.counterevidence_handled
                for c in review.semantic.claims
            ),
            "relations": [r.model_dump(mode="json") for r in review.relations],
            **metrics,
        }
        success = (
            review.task_success
            and review.disposition_correct
            and metrics["disposition_correct"]
            and metrics["required_claim_recall"] in {None, 1.0}
            and metrics["grounded_claim_fraction"] in {None, 1.0}
            and all(c.verdict == "supported" for c in review.citations)
            and all(r.verdict in {"correct", "correctly_unknown"} for r in review.relations)
            and observation["state"] == "completed"
        )
        if case.category == "wrong_relation" and not {
            "excluded_by_scope",
            "applicability_unknown",
        } <= {r.relation_type for r in review.relations}:
            raise ValueError("relationship_review_missing_required_scope_and_unknown_judgments")
    else:
        if review.semantic is not None or review.citations or review.relations:
            raise ValueError("guard_review_cannot_invent_claims_or_evidence")
        success = (
            review.task_success and review.disposition_correct and observation["state"] == "guarded"
        )
    return {
        "status": "reviewed",
        "reviewer_kind": review.reviewer_kind,
        "formal_task_success": bool(success) if review.reviewer_kind == "human" else None,
        "formal_metrics": citation_metrics if review.reviewer_kind == "human" else None,
        "provisional": {"task_success": bool(success), "metrics": citation_metrics}
        if review.reviewer_kind != "human"
        else None,
    }


def export_guard(observation, case, directory):
    identifier = "BLIND-" + content_hash({"observation": observation, "case": case.case_id})[:12]
    directory.mkdir(parents=True, exist_ok=True)
    public = {
        "blind_id": identifier,
        "question": case.request.question,
        "scope": case.scope.model_dump(mode="json"),
        "guard": [
            {k: q[k] for k in ["kind", "fields", "prompt"] if k in q}
            for q in observation.get("guard", [])
        ],
        "execution_failed": observation["state"] == "failed",
    }
    (directory / (identifier + ".json")).write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return identifier

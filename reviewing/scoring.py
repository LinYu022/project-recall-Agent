"""Pure local predicates; observed context coverage is not semantic support."""

from collections import Counter

from ingestion.records import content_hash
from reviewing.contracts import Review
from reviewing.policy import POLICY


def context_links(report):
    cards = {c["evidence_id"]: c for c in report["cards"]}
    aliases = report["aliases"]
    if len(cards) != len(report["cards"]) or not set(aliases.values()) <= cards.keys():
        raise ValueError("invalid_report_card_or_alias_identity")

    def descendants(identifier, path=()):
        if identifier in path or identifier not in cards:
            raise ValueError("cyclic_or_unknown_required_context")
        result = set()
        for child in cards[identifier]["required_context_ids"]:
            result.add(child)
            result |= descendants(child, (*path, identifier))
        return result

    links = []
    for claim in (report.get("proposal") or {}).get("claims", []):
        used = set(claim["support_ids"] + claim["counter_ids"])
        if not used <= aliases.keys():
            raise ValueError("claim_uses_unknown_evidence_alias")
        used_ids = {aliases[a] for a in used}
        for root in sorted(used):
            for identifier in sorted(descendants(aliases[root])):
                links.append(
                    {
                        "claim_id": claim["claim_id"],
                        "root_alias": root,
                        "context_id": identifier,
                        "context_aliases": sorted(a for a, i in aliases.items() if i == identifier),
                        "present": identifier in used_ids,
                    }
                )
    return links


def assess(observation, report, reference, case, value, *, experiment_id, sources, inputs):
    review = Review.model_validate(value)
    if (
        review.experiment_id != experiment_id
        or review.cell_id != observation["cell"]["cell_id"]
        or observation["cell"]["case_id"] != case.case_id
        or review.blind_id != observation["blind_id"]
        or review.observation_sha256 != content_hash(observation)
        or review.reference_sha256 != case.reference_sha256
        or review.reference_sha256 != content_hash(reference.model_dump(mode="json"))
        or review.policy_sha256 != content_hash(POLICY)
        or review.report_sha256 != (content_hash(report) if report is not None else None)
        or review.report_sha256 != observation.get("report_content_sha256")
        or (report is None) != (observation.get("report_reference") is None)
    ):
        raise ValueError("review_exact_observation_report_reference_or_policy_binding_changed")
    for source in review.sources:
        raw = sources.get(source.name)
        if raw is None or content_hash(raw) != source.content_sha256:
            raise ValueError("review_source_missing_or_changed")
        if review.reviewer_kind == "human" and (
            raw.get("reviewer_kind") != "human"
            or raw.get("reviewer_id") != review.reviewer_id
            or raw.get("observation_sha256") != review.observation_sha256
            or raw.get("report_content_sha256") != review.report_sha256
            or raw.get("reference_sha256") != review.reference_sha256
        ):
            raise ValueError("human_judgment_source_identity_or_binding_missing")
    if review.reviewer_kind == "human":
        trusted = [sources[s.name] for s in review.sources]
        for field in ["task_success", "disposition_correct"]:
            actual = {raw[field] for raw in trusted if type(raw.get(field)) is bool}
            supplied_value = getattr(review, field)
            if supplied_value is not None and actual != {supplied_value}:
                raise ValueError("human_task_judgment_not_present_in_trusted_replies")
        if review.complete_task_reading and not any(
            raw.get("complete_task_reading") is True or raw.get("complete_task_review") is True
            for raw in trusted
        ):
            raise ValueError("human_complete_reading_not_recorded_in_trusted_replies")
        for reviewed_claim in review.claims:
            actual = set()
            for raw in trusted:
                if reviewed_claim.claim_id in raw.get("claim_verdicts", {}):
                    actual.add(raw["claim_verdicts"][reviewed_claim.claim_id])
                if raw.get("reviewed_claim_id") == reviewed_claim.claim_id:
                    actual.add(raw["claim_verdict"])
                for item in raw.get("reviewed_claims", []):
                    if item["claim_id"] == reviewed_claim.claim_id:
                        actual.add(item["verdict"])
            if actual != {reviewed_claim.verdict}:
                raise ValueError("human_claim_verdict_not_present_in_trusted_replies")
    if review.input_visibility is not None:
        visibility = review.input_visibility
        raw = inputs.get(visibility.input_artifact_sha256)
        if raw is None or content_hash(raw) != visibility.input_content_sha256:
            raise ValueError("input_visibility_not_bound_to_verified_cell_input")

    raw_claims = ((report or {}).get("proposal") or {}).get("claims", [])
    claims = {c["claim_id"]: c for c in raw_claims}
    if len(claims) != len(raw_claims):
        raise ValueError("duplicate_report_claims")
    pairs = {
        (identifier, alias)
        for identifier, claim in claims.items()
        for alias in claim["support_ids"] + claim["counter_ids"]
    }
    supplied = {(c.claim_id, c.evidence_alias) for c in review.citations}
    if not supplied <= pairs or not {c.claim_id for c in review.claims} <= claims.keys():
        raise ValueError("review_uses_unknown_claim_or_citation")
    links = context_links(report) if report is not None else []
    required = {(link["claim_id"], a) for link in links for a in link["context_aliases"]}
    for citation in review.citations:
        pair = (citation.claim_id, citation.evidence_alias)
        claim = claims[citation.claim_id]
        if citation.role in {"required_context", "both"} and pair not in required:
            raise ValueError("context_role_requires_actual_required_context_dependency")
        if (
            citation.role in {"direct", "both"}
            and citation.evidence_alias not in claim["support_ids"]
        ):
            raise ValueError("counter_only_alias_cannot_be_scored_as_positive_support")
        if (
            citation.role == "counterevidence"
            and citation.evidence_alias not in claim["counter_ids"]
        ):
            raise ValueError("counterevidence_role_requires_counter_reference")

    obligation_ids = {c.reference_claim_id for c in reference.required_claims}
    if not {o.reference_claim_id for o in review.obligations} <= obligation_ids:
        raise ValueError("undefined_reference_obligation")
    for obligation in review.obligations:
        if not set(obligation.claim_ids) <= claims.keys():
            raise ValueError("obligation_uses_unknown_answer_claim")
        used = {a for identifier in obligation.claim_ids for a in claims[identifier]["support_ids"]}
        if not set(obligation.support_ids) <= used:
            raise ValueError("obligation_evidence_not_cited_in_its_answer_claims")
        if obligation.verdict == "satisfied" and obligation.evidence_basis == "frozen_slots":
            from investigation.scoring import matches_slot

            cards = {c["evidence_id"]: c for c in report["cards"]}
            evidence = [cards[report["aliases"][a]] for a in obligation.support_ids]
            slots = {
                s.slot_id for s in reference.slots if any(matches_slot(c, s) for c in evidence)
            }
            target = next(
                c
                for c in reference.required_claims
                if c.reference_claim_id == obligation.reference_claim_id
            )
            if not any(set(group) <= slots for group in target.evidence_sets):
                raise ValueError("obligation_evidence_does_not_meet_frozen_OR_of_AND_slots")
    covered: dict[str, set[int]] = {identifier: set() for identifier in claims}
    for assertion in review.assertions:
        claim = claims.get(assertion.claim_id)
        if (
            claim is None
            or assertion.end > len(claim["statement"])
            or claim["statement"][assertion.start : assertion.end] != assertion.text
        ):
            raise ValueError("assertion_must_match_exact_claim_span")
        if covered[assertion.claim_id] & set(range(assertion.start, assertion.end)):
            raise ValueError("atomic_assertion_spans_must_not_overlap_or_repeat")
        covered[assertion.claim_id].update(range(assertion.start, assertion.end))
    if review.assertions_exhaustive and any(
        any(
            ch.isalnum() and i not in covered[identifier] for i, ch in enumerate(claim["statement"])
        )
        for identifier, claim in claims.items()
    ):
        raise ValueError("exhaustive_assertions_do_not_cover_every_claim")

    settled = review.complete_task_reading and review.dispute_status != "pending"
    citation_ready = (
        settled
        and supplied == pairs
        and all(
            c.role != "unresolved"
            and c.direct_verdict != "not_evaluable"
            and c.counter_verdict != "not_evaluable"
            for c in review.citations
        )
    )
    directs = [c for c in review.citations if c.role in {"direct", "both"}]
    assertions_ready = (
        settled
        and review.assertions_exhaustive
        and all(a.verdict != "not_evaluable" for a in review.assertions)
    )
    obligations_ready = (
        settled
        and {o.reference_claim_id for o in review.obligations} == obligation_ids
        and all(o.verdict != "not_evaluable" for o in review.obligations)
    )
    required_relations = (
        {"excluded_by_scope", "applicability_unknown"}
        if case.category == "wrong_relation"
        else set()
    )
    relations_ready = (
        settled
        and required_relations <= {r.relation_type for r in review.relations}
        and all(r.verdict != "not_evaluable" for r in review.relations)
    )
    if report is None and (
        review.claims
        or review.citations
        or review.assertions
        or review.obligations
        or review.relations
    ):
        raise ValueError("no_report_review_cannot_invent_semantic_items")
    if review.task_success is True:
        positive = (
            settled
            and review.disposition_correct is True
            and observation["state"] in {"completed", "guarded"}
        )
        if report is not None:
            positive &= (
                bool(claims)
                and observation["state"] == "completed"
                and report.get("execution_status") == "completed"
                and (report.get("evidence_verification") or {}).get("status") == "verified"
                and (report.get("claim_verification") or {}).get("status") == "passed"
                and citation_ready
                and assertions_ready
                and obligations_ready
                and relations_ready
                and {c.claim_id for c in review.claims} == claims.keys()
                and all(
                    c.verdict == "supported" and c.scope_verdict == "correct" for c in review.claims
                )
                and all(c.direct_verdict == "supported" for c in directs)
                and all(
                    c.counter_verdict == "supported"
                    for c in review.citations
                    if c.role == "counterevidence"
                )
                and all(a.verdict == "supported" for a in review.assertions)
                and all(o.verdict == "satisfied" for o in review.obligations)
                and all(r.verdict in {"correct", "correctly_unknown"} for r in review.relations)
                and all(link["present"] for link in links)
            )
        if not positive:
            raise ValueError("positive_task_verdict_requires_complete_positive_obligation_review")
    metrics = {
        "task_success": review.task_success if settled else None,
        "disposition_correct": review.disposition_correct if settled else None,
        "reviewed_claim_count": len(review.claims),
        "reported_claim_verdict_counts": dict(Counter(c.verdict for c in review.claims)),
        "claim_judgments_complete": settled
        and {c.claim_id for c in review.claims} == claims.keys()
        and all(c.verdict != "not_evaluable" for c in review.claims),
        "citation_pairs_expected": len(pairs),
        "citation_pairs_reviewed": len(review.citations),
        "citation_roles_complete": citation_ready,
        "context_only_citation_pairs": sum(c.role == "required_context" for c in review.citations),
        "direct_citation_pairs": len(directs),
        "direct_citation_verdict_counts": dict(Counter(c.direct_verdict for c in directs)),
        "direct_citation_aspect_support_rate": sum(c.direct_verdict == "supported" for c in directs)
        / len(directs)
        if citation_ready and directs
        else None,
        "direct_metric_scope": "reviewer_named_aspect_not_necessarily_all_assertions_in_claim",
        "whole_claim_citation_support_rate": None,
        "required_context_links_expected": len(links),
        "required_context_links_present": sum(link["present"] for link in links),
        "required_context_coverage": sum(link["present"] for link in links) / len(links)
        if links
        else None,
        "context_coverage_is_mechanical_not_semantic": True,
        "atomic_assertions_exhaustive": assertions_ready,
        "reviewed_atomic_assertion_count": len(review.assertions),
        "unsupported_atomic_assertion_count": sum(
            a.verdict == "unsupported" for a in review.assertions
        ),
        "partial_atomic_assertion_count": sum(a.verdict == "partial" for a in review.assertions),
        "unsupported_atomic_assertion_rate": sum(
            a.verdict == "unsupported" for a in review.assertions
        )
        / len(review.assertions)
        if assertions_ready and review.assertions
        else None,
        "partial_atomic_assertion_rate": sum(a.verdict == "partial" for a in review.assertions)
        / len(review.assertions)
        if assertions_ready and review.assertions
        else None,
        "required_obligations_reviewed": len(review.obligations),
        "required_obligations_expected": len(obligation_ids),
        "required_obligation_recall": sum(o.verdict == "satisfied" for o in review.obligations)
        / len(obligation_ids)
        if obligations_ready and obligation_ids
        else None,
        "relations_ready": relations_ready,
        "relation_judgments": [r.model_dump(mode="json") for r in review.relations],
    }
    return {
        "schema_version": "role-aware-assessment/0.2",
        "policy_sha256": review.policy_sha256,
        "reviewer_kind": review.reviewer_kind,
        "status": "reviewed_task" if settled and review.task_success is not None else "pending",
        "formal": metrics if review.reviewer_kind == "human" else None,
        "provisional": metrics if review.reviewer_kind != "human" else None,
        "required_context_links": links,
        "analysis_revision_is_post_hoc": True,
    }

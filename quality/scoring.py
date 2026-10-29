"""Mechanical completeness gates for model judgments; never relabel them human."""

from collections import Counter

from quality.contracts import Judgment

RELATIONS = {
    "scope_overlap",
    "symptom_similar",
    "officially_referenced",
    "excluded_by_scope",
    "applicability_unknown",
}


def unique(values, label):
    if len(values) != len(set(values)):
        raise ValueError("duplicate_" + label)


def validate_judgment(payload, value):
    result = Judgment.model_validate(value)
    claims = {c["claim_id"]: c for c in (payload.get("proposal") or {}).get("claims", [])}
    unique([c.claim_id for c in result.claims], "claim")
    if {c.claim_id for c in result.claims} != set(claims):
        raise ValueError("incomplete_claim_denominator")
    expected = {
        (c["claim_id"], a) for c in claims.values() for a in c["support_ids"] + c["counter_ids"]
    }
    supplied = [(c.claim_id, c.evidence_alias) for c in result.citations]
    unique(supplied, "citation")
    if set(supplied) != expected:
        raise ValueError("incomplete_citation_denominator")
    required = {tuple(x) for x in payload["program_context_pairs"]}
    atoms = {}
    for claim in result.claims:
        original = claims[claim.claim_id]
        cursor = 0
        covered: set[int] = set()
        for atom in claim.atoms:
            if atom.atom_id in atoms:
                raise ValueError("duplicate_atom")
            start = original["statement"].find(atom.quote, cursor)
            if start < 0:
                raise ValueError("atom_quote_not_exact_ordered_nonoverlapping")
            end = start + len(atom.quote)
            covered.update(range(start, end))
            if not set(atom.support_ids) <= set(original["support_ids"]):
                raise ValueError("atom_adds_unlisted_support")
            atoms[atom.atom_id] = (claim.claim_id, start, end, atom)
            cursor = end
        if any(ch.isalnum() and i not in covered for i, ch in enumerate(original["statement"])):
            raise ValueError("atomic_review_leaves_uncovered_text")
    for citation in result.citations:
        original = claims[citation.claim_id]
        if (
            citation.role in {"required_context", "both"}
            and (citation.claim_id, citation.evidence_alias) not in required
        ):
            raise ValueError("invented_context_role")
        if (
            citation.role == "counterevidence"
            and citation.evidence_alias not in original["counter_ids"]
        ):
            raise ValueError("invented_counter_role")
        if citation.role in {"direct", "both"}:
            if citation.evidence_alias not in original["support_ids"] or not citation.atom_ids:
                raise ValueError("direct_role_missing_actual_aspect")
            if citation.aspect_verdict is None or citation.whole_claim_verdict is None:
                raise ValueError("direct_role_missing_support_judgment")
        if citation.role == "required_context" and (
            citation.atom_ids
            or citation.aspect_verdict is not None
            or citation.whole_claim_verdict is not None
        ):
            raise ValueError("context_only_invents_semantic_support")
        for key in citation.atom_ids:
            if key not in atoms or atoms[key][0] != citation.claim_id:
                raise ValueError("citation_uses_other_or_unknown_atom")
    obligation_ids = {o["obligation_id"] for o in payload["obligations"]}
    unique([o.obligation_id for o in result.obligations], "obligation")
    if {o.obligation_id for o in result.obligations} != obligation_ids:
        raise ValueError("incomplete_obligation_denominator")
    for obligation in result.obligations:
        if not set(obligation.claim_ids) <= claims.keys():
            raise ValueError("obligation_adds_claim")
        used = {a for c in obligation.claim_ids for a in claims[c]["support_ids"]}
        if not set(obligation.support_ids) <= used:
            raise ValueError("obligation_adds_uncited_evidence")
        if (
            obligation.verdict == "satisfied"
            and claims
            and (not obligation.claim_ids or not obligation.support_ids)
        ):
            raise ValueError("satisfied_answer_obligation_missing_grounding")
    unique([r.relation_type for r in result.relations], "relation")
    if {r.relation_type for r in result.relations} != RELATIONS:
        raise ValueError("incomplete_relation_type_denominator")
    required_relations = set(payload["required_relation_types"])
    if any(
        r.relation_type in required_relations and r.verdict == "not_applicable"
        for r in result.relations
    ):
        raise ValueError("required_relation_evaded_as_inapplicable")
    return result


def metrics(payload, value):
    result = validate_judgment(payload, value)
    atoms = [a for c in result.claims for a in c.atoms]
    direct = [c for c in result.citations if c.role in {"direct", "both"}]
    unresolved = (
        not result.complete_reading
        or result.disposition_correct is None
        or result.core_complete is None
        or result.all_requirements == "not_evaluable"
        or any(
            c.verdict == "not_evaluable" or c.scope_verdict == "not_evaluable"
            for c in result.claims
        )
        or any(a.verdict == "not_evaluable" for a in atoms)
        or any(
            c.role == "unresolved" or c.aspect_verdict == "not_evaluable" for c in result.citations
        )
        or any(o.verdict == "not_evaluable" for o in result.obligations)
        or any(r.verdict == "not_evaluable" for r in result.relations)
    )
    strict = (
        not unresolved
        and payload["execution_state"] in {"completed", "guarded"}
        and result.disposition_correct is True
        and result.core_complete is True
        and result.all_requirements == "satisfied"
        and all(c.verdict == "supported" and c.scope_verdict == "correct" for c in result.claims)
        and all(a.verdict == "supported" for a in atoms)
        and all(c.aspect_verdict == "supported" for c in direct)
        and all(o.verdict == "satisfied" for o in result.obligations)
        and all(
            r.verdict in {"correct", "correctly_unknown", "not_applicable"}
            for r in result.relations
        )
    )
    return {
        "provenance": "model_judgment_not_human",
        "complete_components": not unresolved,
        "strict_success": strict if not unresolved else None,
        "disposition_correct": result.disposition_correct,
        "core_complete": result.core_complete,
        "all_requirements": result.all_requirements,
        "atomic_verdict_counts": dict(Counter(a.verdict for a in atoms)),
        "direct_aspect_counts": dict(Counter(c.aspect_verdict for c in direct)),
        "whole_claim_pair_counts": dict(Counter(c.whole_claim_verdict for c in direct)),
        "context_only_pairs": sum(c.role == "required_context" for c in result.citations),
        "relation_judgments": [r.model_dump() for r in result.relations],
    }

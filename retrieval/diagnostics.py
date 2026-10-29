"""Diagnose candidate loss using prelabelled targets; never invent missing relevance labels."""


def diagnose(index: dict, result: dict, relevant_keys: set[str], cutoff: int) -> dict:
    documents = index["corpus"]["documents"]
    target_ids = {d["document_id"] for d in documents if d["evidence_key"] in relevant_keys}
    if not target_ids:
        return {"diagnosis": "not_indexed", "target_documents": 0}
    scopes = {r["document_id"]: r for r in result["scope_trace"]}
    matched = {i for i in target_ids if scopes.get(i, {}).get("channel") == "matched"}
    pending = {i for i in target_ids if scopes.get(i, {}).get("channel") == "pending"}
    if not matched:
        return {
            "diagnosis": "pending_scope_verification" if pending else "filtered_or_not_allowed",
            "target_documents": len(target_ids),
            "scope_reasons": sorted(
                {reason for i in target_ids for reason in scopes.get(i, {}).get("reasons", [])}
            ),
        }
    by_id = {d["document_id"]: d["evidence_key"] for d in documents}
    candidates = {
        by_id[c["document_id"]]
        for stage in result["candidate_trace"]
        if stage["channel"] == "matched" and stage["stage"] in {"bm25", "dense"}
        for c in stage["candidates"]
    }
    final = [h["evidence_key"] for h in result["matched_hits"]]
    if relevant_keys & set(final[:cutoff]):
        status = "retrieved"
    elif relevant_keys & candidates:
        status = "ranked_below_cutoff"
    else:
        status = "not_retrieved"
    ranks = [rank for rank, key in enumerate(final, 1) if key in relevant_keys]
    return {
        "diagnosis": status,
        "target_documents": len(target_ids),
        "candidate_target_groups": len(relevant_keys & candidates),
        "best_returned_rank": min(ranks) if ranks else None,
        "cutoff": cutoff,
    }

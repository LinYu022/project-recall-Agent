"""The only executable procedure is a bounded current-release scope inspection."""

from ingestion.records import content_hash
from investigation.evidence import structured_card, text_cards, verify_cards
from memory.procedure_contracts import compile_procedure, missing_preconditions
from releases.contracts import ReleaseQuery


def event_groups(cards):
    import json
    from pathlib import Path

    registry = json.loads(Path("examples/procedure-event-families.json").read_bytes())["groups"]
    return sorted(
        {
            registry.get(r["campaign_number"], "campaign:" + r["campaign_number"])
            for c in cards
            if c["kind"] == "structured_lookup"
            for r in c["payload"].get("rows", [])
            if r["channel"] == "matched" and r.get("campaign_number")
        }
    )


def execute_scope_recipe(
    service, context, request, definition, config, store, invoke, *, seed=None, after_lookup=None
):
    compile_procedure(definition)
    missing = missing_preconditions(
        definition, context.scope, request, context.manifest["release_id"]
    )
    if missing:
        raise ValueError("procedure_preconditions_not_met:" + ",".join(missing))
    query = ReleaseQuery(
        task_id=request.task_id,
        expected_release_id=context.manifest["release_id"],
        query=request.retrieval_query,
        method=config.retrieval_method,
        top_k=config.retrieve_k,
    )
    args = {"request": query.model_dump(mode="json")}
    cards = []

    def call(tool, arguments, operation):
        output = invoke(tool, arguments, operation)
        if output is None:
            raise ValueError("procedure_readonly_operation_unavailable")
        origin = store.put_json({"tool": tool, "input": arguments, "output": output})
        return output, origin["sha256"]

    if seed:
        verify_cards(context, store, [seed])
        cards.append(seed)
        lookup = seed.payload
    else:
        lookup, origin = call("lookup", args, context.lookup)
        cards.append(structured_card(context, origin, lookup, "structured_lookup"))
    groups = event_groups([c.model_dump(mode="json") for c in cards])
    if after_lookup:
        after_lookup(groups)
    status = "insufficient_evidence"
    if lookup["status"] == "complete" and lookup["counts"]["matched"] == 0:
        status = "no_matching_records_in_allowed_snapshot"
    else:
        found, _ = call("search", args, lambda: service.query(query))
        candidates = found["matched_hits"] or found["pending_hits"]
        if candidates:
            document = candidates[0]["document_id"]
            output, origin = call(
                "expand",
                args | {"document_id": document, "include_related": False},
                lambda: service.query(query, document_id=document, include_related=False),
            )
            cards.extend(text_cards(context, origin, output))
            status = "scope_inspected" if found["matched_hits"] else "scope_pending"
    verification = verify_cards(context, store, cards)
    result = {
        "status": status,
        "event_groups": groups,
        "scope": context.scope.model_dump(mode="json"),
        "release_id": context.manifest["release_id"],
        "verification": verification,
        "individual_applicability": "not_confirmed",
        "causality": "not_confirmed",
        "required_context_preserved": True,
        "evidence_ids": [c.evidence_id for c in cards],
        "cards_sha256": content_hash([c.model_dump(mode="json") for c in cards]),
        "is_final_answer": False,
    }
    return result, cards

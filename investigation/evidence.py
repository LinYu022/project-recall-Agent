"""Server-created evidence with replayable origins; models receive short aliases only."""

import json

from ingestion.records import content_hash
from investigation.contracts import EvidenceCard
from parsing.chunks import checked_chunks, cite_chunk, expand_chunk
from runtime.artifacts import ArtifactIntegrityError


def selected(value, path):
    for key in path:
        value = value[key]
    return value


def make_card(context, origin, path, payload, kind, channel, sources, versions, dependencies=()):
    # JSON artifacts normalize tuples into arrays. Freeze that representation before comparison.
    payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    stable = (
        {"citations": payload["citations"]}
        if kind == "text"
        else {k: v for k, v in payload.items() if k not in {"task_id", "trace_artifact"}}
    )
    body = {
        "schema_version": "evidence-card/0.1",
        "evidence_key": content_hash([kind, stable]),
        "task_id": context.task["task_id"],
        "release_id": context.manifest["release_id"],
        "kind": kind,
        "origin_sha256": origin,
        "selector": {"path": path},
        "source_ids": sources,
        "source_version_ids": versions,
        "scope_channel": channel,
        "payload": payload,
        "required_context_ids": list(dependencies),
    }
    return EvidenceCard.model_validate(body | {"evidence_id": content_hash(body)})


def text_cards(context, origin, output):
    cards: list[EvidenceCard] = []
    branches: list[tuple[list[str | int], dict]] = [(["focus"], output["focus"])]
    for position, item in enumerate(output["related"]):
        branches.append((["related", position], item))
    for branch, expanded in branches:
        doc = context.documents[expanded["document_id"]]
        metadata = doc["metadata"]
        arguments = {
            "kind": "text",
            "channel": expanded["scope_channel"],
            "sources": [metadata["source_id"]],
            "versions": [metadata["source_version_id"]],
        }
        contexts = expanded["bundle"]["context"]
        focus = expanded["bundle"]["focus"]
        nodes = {n["chunk_id"]: n for n in [*contexts, focus]}
        paths = {
            n["chunk_id"]: ["output", *branch, "bundle", "context", i]
            for i, n in enumerate(contexts)
        }
        paths[focus["chunk_id"]] = ["output", *branch, "bundle", "focus"]
        built: dict[str, EvidenceCard] = {}
        active: set[str] = set()

        def build(identifier):
            if identifier in built:
                return built[identifier]
            if identifier in active or identifier not in nodes:
                raise ArtifactIntegrityError("cyclic or missing chunk context")
            active.add(identifier)
            node = nodes[identifier]
            dependencies = list(node["required_context_ids"])
            if identifier == focus["chunk_id"]:
                dependencies.extend(n["chunk_id"] for n in contexts)
            identifiers = [build(d).evidence_id for d in dict.fromkeys(dependencies)]
            result = make_card(
                context, origin, paths[identifier], node, **arguments, dependencies=identifiers
            )
            built[identifier] = result
            active.remove(identifier)
            return result

        # Context cards can themselves carry scope requirements (e.g. a PDF Summary section).
        cards.extend(build(n["chunk_id"]) for n in [*contexts, focus])
    return cards


def structured_card(context, origin, output, kind):
    return make_card(
        context,
        origin,
        ["output"],
        output,
        kind,
        "inspection" if kind == "relationship" else "matched",
        context.sources,
        sorted(context.records),
    )


def verify_cards(context, store, cards):
    """Reconstruct origins from pinned data. A valid hash alone is insufficient."""
    expansions = {}
    replayed = {}
    for card in cards:
        body = card.model_dump(mode="json", exclude={"evidence_id"})
        if content_hash(body) != card.evidence_id:
            raise ArtifactIntegrityError("evidence identity differs")
        if (
            card.task_id != context.task["task_id"]
            or card.release_id != context.manifest["release_id"]
            or not set(card.source_ids) <= set(context.sources)
            or not set(card.source_version_ids) <= set(context.records)
        ):
            raise ArtifactIntegrityError("evidence outside task release/source/time selection")
        origin = store.read_json(card.origin_sha256)
        if selected(origin, card.selector["path"]) != card.payload:
            raise ArtifactIntegrityError("evidence differs from tool origin")
        if card.kind == "text":
            if origin["tool"] != "expand":
                raise ArtifactIntegrityError("text requires expansion origin")
            if card.origin_sha256 not in expansions:
                from releases.contracts import ReleaseQuery

                query = ReleaseQuery.model_validate(origin["input"]["request"])
                output = origin["output"]
                if (
                    query.task_id != context.task["task_id"]
                    or query.expected_release_id != card.release_id
                    or output["task_id"] != card.task_id
                    or output["release_id"] != card.release_id
                    or origin["input"]["document_id"] != output["focus"]["document_id"]
                    or (output["related"] and not origin["input"]["include_related"])
                ):
                    raise ArtifactIntegrityError("expanded origin binding differs")
                expected = []
                for branch in [origin["output"]["focus"], *origin["output"]["related"]]:
                    doc = context.documents[branch["document_id"]]
                    # Replays bytes, parser, chunk recipe, scope and required context.
                    from retrieval.scope import classify_scope

                    channel = classify_scope(doc["metadata"], context.scope)[0]
                    if channel == "excluded" or doc["metadata"]["source_id"] not in context.sources:
                        raise ArtifactIntegrityError("expanded text outside eligible scope")
                    chunks = checked_chunks(
                        context.base / "raw",
                        context.base / "parsed",
                        context.base / "chunks",
                        doc["chunk_artifact_sha256"],
                    )
                    bundle = expand_chunk(chunks, doc["chunk_id"])
                    for node in [bundle["focus"], *bundle["context"]]:
                        node["citations"] = cite_chunk(
                            context.base / "raw", context.base / "parsed", chunks, node["chunk_id"]
                        )
                    if bundle != branch["bundle"] or channel != branch["scope_channel"]:
                        raise ArtifactIntegrityError("expanded origin differs from source replay")
                    expected.append(branch["document_id"])
                focus = context.documents[expected[0]]
                related = set(expected[1:])
                if any(
                    context.documents[d]["metadata"].get("campaign_number")
                    not in focus["metadata"]["official_references"]
                    for d in related
                ):
                    raise ArtifactIntegrityError("unapproved related expansion")
                expansions[card.origin_sha256] = {
                    c.evidence_id for c in text_cards(context, card.origin_sha256, origin["output"])
                }
            if card.evidence_id not in expansions[card.origin_sha256]:
                raise ArtifactIntegrityError("card metadata or context differs from replay")
        else:
            if card.origin_sha256 not in replayed:
                from releases.contracts import ReleaseQuery

                query = ReleaseQuery.model_validate(origin["input"]["request"])
                if (
                    query.task_id != context.task["task_id"]
                    or query.expected_release_id != card.release_id
                ):
                    raise ArtifactIntegrityError("tool input outside frozen task")
                if card.kind == "structured_lookup" and origin["tool"] == "lookup":
                    output = context.lookup()
                elif card.kind == "count" and origin["tool"] == "count":
                    output = context.service.count(query, origin["input"]["components"])
                elif card.kind == "relationship" and origin["tool"] == "links":
                    from entities.scopes import RecordLocator

                    output = context.service.links(
                        query,
                        RecordLocator.model_validate(origin["input"]["left"]),
                        RecordLocator.model_validate(origin["input"]["right"]),
                    )
                else:
                    raise ArtifactIntegrityError("evidence kind differs from tool")
                if content_hash(output) != content_hash(origin["output"]):
                    raise ArtifactIntegrityError("structured evidence differs from query replay")
                expected_card = structured_card(context, card.origin_sha256, output, card.kind)
                replayed[card.origin_sha256] = expected_card.evidence_id
            if card.evidence_id != replayed[card.origin_sha256]:
                raise ArtifactIntegrityError("structured card differs from replay")
    ids = {c.evidence_id for c in cards}
    if any(not set(c.required_context_ids) <= ids for c in cards):
        raise ArtifactIntegrityError("required evidence context missing")
    return {
        "status": "verified",
        "cards_checked": len(cards),
        "origin_replays": len(expansions) + len(replayed),
        "semantic_support": "requires_separate_review",
    }


def verify_proposal(proposal, cards, aliases, scope):
    lookup = {c.evidence_id: c for c in cards}
    for claim in proposal.claims:
        if claim.scope != scope:
            raise ValueError("claim scope differs from immutable task scope")
        if claim.kind in {"individual_applicability", "causal"}:
            raise ValueError("no individual vehicle or causal confirmation tool")
        identifiers = claim.support_ids + claim.counter_ids
        if any(a not in aliases for a in identifiers):
            raise ValueError("unknown evidence alias")
        used = {aliases[a] for a in identifiers}
        support = [lookup[aliases[a]] for a in claim.support_ids]
        if any(not set(lookup[i].required_context_ids) <= used for i in used):
            raise ValueError("claim omits mandatory parent/scope context")
        if claim.kind not in {"scope_exclusion", "relationship", "insufficiency"} and any(
            c.scope_channel != "matched" for c in support
        ):
            raise ValueError("pending/excluded/inspection evidence cannot confirm scope")
        required_kind = {
            "count": "count",
            "relationship": "relationship",
            "source_fact": "text",
        }.get(claim.kind)
        if required_kind and not any(c.kind == required_kind for c in support):
            raise ValueError("claim lacks evidence of its declared kind")
        if claim.kind == "scope_exclusion" and not any(
            c.kind in {"structured_lookup", "relationship"} for c in support
        ):
            raise ValueError("scope exclusion requires structured scope evidence")
    return {
        "status": "passed",
        "claims_checked": len(proposal.claims),
        "semantic_support": "not_evaluated_by_program",
    }


def close_context(proposal, cards, aliases):
    """Attach already-visible mandatory context, preserving the unmodified model output separately."""
    result = proposal.model_copy(deep=True)
    lookup = {c.evidence_id: c for c in cards}
    inverse = {v: k for k, v in aliases.items()}
    changes = []
    for claim in result.claims:
        pending = list(claim.support_ids + claim.counter_ids)
        added = []
        while pending:
            alias = pending.pop()
            if alias not in aliases:
                continue  # Strict verification rejects unknown aliases afterward.
            for identifier in lookup[aliases[alias]].required_context_ids:
                dependency = inverse[identifier]
                if dependency not in claim.support_ids + claim.counter_ids:
                    claim.support_ids.append(dependency)
                    pending.append(dependency)
                    added.append(dependency)
        if added:
            changes.append({"claim_id": claim.claim_id, "added_context_aliases": added})
    return result, changes

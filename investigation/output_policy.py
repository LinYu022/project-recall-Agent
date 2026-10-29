"""Narrow publication guards, not an automated semantic judge or evaluation reference."""

import re

VERSION = "local-evidence-output/0.1"
GUIDANCE = """
Check each conclusion against ONLY its own support_ids; citations in earlier conclusions do not
transfer. Repeat an alias locally when needed. A risk card cannot support a trigger from Summary.
For each compared source, explicitly state the requested Summary, Consequence and Remedy;
do not replace crash risks with just the defect or the fix. Preserve 'certain' as 部分/特定.
Keep model/submodel, configuration, activation and parking conditions, distinct warnings and every
conditional remedy (including rework). Prefer requested content over optional dates/phone numbers.
At most 8 concise claims. Independent facts may need separate claims; do not merge just to save slots.
Relationship cards marked candidate, with no supporting_evidence or allowed_wording, cannot prove
an official reference/replacement or concrete shared symptoms. Cite the actual text locally for
those facts; otherwise explicitly keep the relation unknown. Shared scope never proves same defect.
Use 'relationship' with a relationship card; use 'source_fact' with matched text for a textual
comparison without a relationship card. Do not add a card merely to satisfy a kind constraint.
These mechanical rules do not establish semantic support. Never claim program-verified truth.
"""


def source_fields(card):
    """Source field names come from replayed locators, never model-assigned facet labels."""
    return sorted(
        {
            r["field_pointer"].rsplit("/", 1)[-1]
            for r in card.payload.get("ranges", [])
            if r.get("field_pointer")
        }
    )


def requested_fields(question):
    patterns = {
        "Summary": r"缺陷|触发|适用范围|defect|trigger|scope",
        "Consequence": r"风险|后果|risk|consequence",
        "Remedy": r"补救|修复措施|remed(?:y|ies)",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, question, re.I)]


def answer_checklist(question, cards, aliases):
    by_id = {c.evidence_id: c for c in cards}
    return {
        "version": VERSION,
        "requested_source_fields": requested_fields(question),
        "visible_source_fields": [
            {"alias": alias, "source_ids": c.source_ids, "fields": source_fields(c)}
            for alias, identifier in aliases.items()
            if (c := by_id[identifier]).kind == "text" and c.scope_channel == "matched"
        ],
        "local_citations_required": True,
        "candidate_relations_are_not_confirmed": True,
        "semantic_support_still_requires_review": True,
    }


def check_answer(proposal, cards, aliases, question):
    """Reject specific, observable citation/coverage failures without repairing a claim."""
    by_id = {c.evidence_id: c for c in cards}
    findings = []
    covered: dict[str, set[str]] = {}
    available: dict[str, set[str]] = {}
    for card in cards:
        if card.kind == "text" and card.scope_channel == "matched":
            for sid in card.source_ids:
                available.setdefault(sid, set()).update(source_fields(card))

    for claim in proposal.claims:
        # verify_proposal runs first. Counter citations and mandatory context do not
        # silently become direct support for this claim's extra factual assertions.
        support = [by_id[aliases[a]] for a in claim.support_ids]
        texts = [c for c in support if c.kind == "text" and c.scope_channel == "matched"]
        text = "\n".join(c.payload.get("text", "") for c in texts)
        statement = claim.statement
        relation_quotes = "\n".join(
            str(e.get("quote", ""))
            for c in support
            if c.kind == "relationship"
            for link in c.payload.get("assessment", {}).get("links", [])
            if link.get("confirmation_status") == "confirmed"
            and link.get("relation_type") == "symptom_similar"
            for e in link.get("supporting_evidence", [])
        )
        if claim.kind == "source_fact":
            for card in texts:
                for sid in card.source_ids:
                    fields = set(source_fields(card))
                    # A naked Consequence citation under a repair-only sentence is
                    # not explicit risk coverage. This is a narrow lexical guard,
                    # not proof of a complete causal chain or faithful translation.
                    if "Consequence" in fields and not re.search(
                        r"风险|碰撞|事故|受伤|伤害|死亡|火灾|起火|失控|制动距离|停车距离|停车所需距离|"
                        r"risk|crash|injur|fire|death|collision|stop.*distance|loss.*control",
                        statement,
                        re.I,
                    ):
                        fields.remove("Consequence")
                    covered.setdefault(sid, set()).update(fields)
            if (
                any("Summary" in source_fields(c) for c in texts)
                and re.search(r"\bcertain\b", text, re.I)
                and re.search(r"(?:19|20)\d{2}", statement)
                and not re.search(r"部分|特定|某些|\bcertain\b", statement, re.I)
            ):
                findings.append({"claim_id": claim.claim_id, "code": "certain_qualifier_missing"})

        # These literal technical markers are a conservative local-citation check,
        # not a test that an arbitrary Chinese paraphrase follows from English.
        for marker in ("LFB", "IEB", "VCU", "TPMS", "ABS", "DSC", "4WD", "OTA"):
            if re.search(rf"(?<![a-z0-9]){marker}(?![a-z0-9])", statement, re.I) and not re.search(
                rf"(?<![a-z0-9]){marker}(?![a-z0-9])", text, re.I
            ):
                # Structured cards can describe the literal term too. Empty candidate
                # facts/tags cannot support named symptoms merely through a component.
                if claim.kind == "source_fact" or texts:
                    findings.append(
                        {
                            "claim_id": claim.claim_id,
                            "code": "local_text_marker_missing",
                            "marker": marker,
                        }
                    )
                elif re.search(
                    r"相似症状|共有症状|共同症状|similar symptoms|shared symptoms", statement, re.I
                ) and not re.search(rf"(?<![a-z0-9]){marker}(?![a-z0-9])", relation_quotes, re.I):
                    findings.append(
                        {
                            "claim_id": claim.claim_id,
                            "code": "symptom_text_citation_missing",
                            "marker": marker,
                        }
                    )

        relation_type = None
        if re.search(
            r"取代|替代|官方互引|官方引用|replac(?:e|es|ed)|supersed|official reference",
            statement,
            re.I,
        ):
            relation_type = "officially_referenced"
        elif re.search(
            r"相似症状|共有症状|共同症状|similar symptoms|shared symptoms", statement, re.I
        ):
            relation_type = "symptom_similar"
        if relation_type and not texts:
            links = [
                link
                for c in support
                if c.kind == "relationship"
                for link in c.payload.get("assessment", {}).get("links", [])
                if link.get("relation_type") == relation_type
            ]
            typed_quotes = "\n".join(
                str(e.get("quote", ""))
                for link in links
                if link.get("confirmation_status") == "confirmed"
                for e in link.get("supporting_evidence", [])
            )
            # Unknown/candidate wording is allowed. A confident detail needs actual
            # evidence; merely including a relation name is never sufficient.
            relation_pattern = (
                r"取代|替代|官方互引|官方引用|replac(?:e|es|ed)|supersed|official reference"
                if relation_type == "officially_referenced"
                else r"相似症状|共有症状|共同症状|similar symptoms|shared symptoms"
            )
            relation_clauses = [
                s
                for s in re.split(r"[，,；;。]", statement)
                if re.search(relation_pattern, s, re.I)
            ]
            unknown = relation_clauses and all(
                re.search(r"未确认|尚未|未知|候选|不能确认|unconfirmed|unknown|candidate", s, re.I)
                for s in relation_clauses
            )
            if not unknown and not any(
                link.get("confirmation_status") == "confirmed"
                and link.get("supporting_evidence")
                and link.get("allowed_wording")
                for link in links
            ):
                findings.append(
                    {
                        "claim_id": claim.claim_id,
                        "code": "unconfirmed_relation_asserted",
                        "relation_type": relation_type,
                    }
                )
            elif (
                not unknown
                and re.search(r"取代|替代|replac(?:e|es|ed)|supersed", statement, re.I)
                and not re.search(r"取代|替代|\breplac(?:e|es|ed)\b|supersed", typed_quotes, re.I)
            ):
                findings.append(
                    {"claim_id": claim.claim_id, "code": "replacement_text_citation_missing"}
                )

    if proposal.disposition == "answered":
        for sid, fields in available.items():
            # Once a source is identified as a recall narrative, a requested
            # field missing from retrieval remains a gap, never an exemption.
            if not fields & {"Summary", "Consequence", "Remedy"}:
                continue  # Other document types do not use these API field names.
            for field in set(requested_fields(question)) - covered.get(sid, set()):
                findings.append(
                    {"source_id": sid, "code": "requested_source_field_not_cited", "field": field}
                )
    return {
        "version": VERSION,
        "status": "rejected" if findings else "passed_narrow_checks",
        "findings": findings,
        "semantic_support": "not_evaluated_by_program",
    }


def decision_format(available_tools):
    """Compact action-specific arguments; omit unused optional fields instead of guessing values."""
    arguments = {
        "search": {"query": "nonempty English search string"},
        "expand": {"document_id": "D001", "include_related": True},
        "count": {},
        "links": {},
        "read": {"evidence_alias": "E001", "offset": 0, "page_size": 2000},
        "batch": {
            "batch": "exactly two distinct available independent actions; only search/expand/count/links"
        },
        "history_search": {"history_query": "nonempty query"},
        "history_window": {
            "history_episode_id": "registered episode",
            "history_event_id": "registered event",
        },
        "apply_skill": {},
        "finish": {"proposal": "AnswerProposal; exact task scope in each claim"},
    }
    return {
        "required": {
            "action": "one available_tools value",
            "reason": "short string",
            "hypotheses": [],
            "evidence_gaps": [],
            "next_steps": [],
        },
        "action_arguments": {a: arguments[a] for a in available_tools},
        "unused_optional_fields": "omit; never emit empty strings, empty objects or null for integer pagination fields",
        "registered_handles_only": "D/E/A examples must be replaced with a currently registered candidate/evidence/artifact; read uses exactly one of evidence_alias or artifact_id",
        "no_extra_fields": True,
    }

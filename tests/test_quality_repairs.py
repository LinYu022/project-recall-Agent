"""Synthetic counterexamples for new development guards, never new human quality scores."""

from copy import deepcopy

import pytest

from ingestion.records import content_hash
from investigation.agent_contracts import Decision
from investigation.communication import bounded_stop, clarification_prompt
from investigation.contracts import AnswerProposal, EvidenceCard
from investigation.evidence import verify_proposal
from investigation.flow import prompt_cards
from investigation.output_policy import answer_checklist, check_answer, decision_format
from retrieval.contracts import SearchScope

H = "a" * 64


def text_card(field, text, source="SOURCE-A"):
    body = {
        "evidence_key": content_hash([source, field, text]),
        "task_id": "SYNTHETIC",
        "release_id": H,
        "origin_sha256": H,
        "selector": {},
        "source_ids": [source],
        "source_version_ids": [H],
        "kind": "text",
        "scope_channel": "matched",
        "payload": {
            "text": text,
            "ranges": [{"field_pointer": "/results/0/" + field}],
            "citations": [],
        },
        "required_context_ids": [],
    }
    return EvidenceCard.model_validate(body | {"evidence_id": content_hash(body)})


def relation_card(*, confirmed=False):
    body = text_card("", "").model_dump(mode="json", exclude={"evidence_id"})
    body["kind"] = "relationship"
    body["scope_channel"] = "inspection"
    body["payload"] = {
        "status": "assessed",
        "coverage": "synthetic",
        "endpoint_scope_channels": [],
        "assessment": {
            "left_scope": {
                "official_id": "A",
                "vehicles": [],
                "components": [],
                "structured_evidence": [],
                "facts": [],
                "coverage": "synthetic",
            },
            "right_scope": {
                "official_id": "B",
                "vehicles": [],
                "components": [],
                "structured_evidence": [],
                "facts": [],
                "coverage": "synthetic",
            },
            "links": [
                {
                    "relation_type": r,
                    "confirmation_status": "confirmed" if confirmed else "candidate",
                    "supporting_evidence": [
                        {"quote": "A replaces B; both describe ABS and DSC loss"}
                    ]
                    if confirmed
                    else [],
                    "allowed_wording": "bounded reviewed relation" if confirmed else None,
                }
                for r in ("officially_referenced", "symptom_similar")
            ],
        },
    }
    return EvidenceCard.model_validate(body | {"evidence_id": content_hash(body)})


def proposal(statements, *, kind="source_fact"):
    return AnswerProposal.model_validate(
        {
            "disposition": "answered",
            "claims": [
                {
                    "claim_id": f"C{i:03}",
                    "kind": kind,
                    "statement": statement,
                    "scope": {},
                    "support_ids": ids,
                    "counter_ids": [],
                }
                for i, (statement, ids) in enumerate(statements, 1)
            ],
        }
    )


def checked(proposed, cards, question=""):
    aliases = {f"E{i:03}": c.evidence_id for i, c in enumerate(cards, 1)}
    verify_proposal(proposed, cards, aliases, SearchScope())
    return check_answer(proposed, cards, aliases, question)


def test_trigger_cannot_borrow_an_earlier_conclusions_citation():
    cards = [
        text_card("Summary", "LFB activated may reduce braking"),
        text_card("Consequence", "Longer stopping distance increases crash risk"),
    ]
    bad = proposal(
        [("LFB启用时制动性能可能下降。", ["E001"]), ("LFB启用时会增加碰撞风险。", ["E002"])]
    )
    result = checked(bad, cards)
    assert result["findings"] == [
        {"claim_id": "C002", "code": "local_text_marker_missing", "marker": "LFB"}
    ]
    assert bad.claims[1].support_ids == ["E002"]  # No silent citation attachment.
    good = bad.model_copy(deep=True)
    good.claims[1].support_ids.append("E001")
    assert checked(good, cards)["findings"] == []


@pytest.mark.parametrize("phrase", ["涉及2025款车型。", "涉及2025款车型；不能确认任何个车。"])
def test_missing_certain_is_not_fixed_by_a_separate_disclaimer(phrase):
    cards = [text_card("Summary", "recalling certain 2025 vehicles")]
    assert (
        checked(proposal([(phrase, ["E001"])]), cards)["findings"][0]["code"]
        == "certain_qualifier_missing"
    )
    assert checked(proposal([("涉及部分2025款车型。", ["E001"])]), cards)["findings"] == []


def test_comparison_checks_each_sources_risk_without_treating_remedy_as_risk():
    cards = [
        text_card("Consequence", "crash risk", "A"),
        text_card("Remedy", "free repair", "A"),
        text_card("Consequence", "another crash risk", "B"),
        text_card("Remedy", "another free repair", "B"),
    ]
    bad = proposal([("A补救。", ["E002"]), ("B补救。", ["E004"])])
    findings = checked(bad, cards, "请对照风险和补救。并非评分标签。 ")["findings"]
    assert {(f["source_id"], f["field"]) for f in findings} == {
        ("A", "Consequence"),
        ("B", "Consequence"),
    }
    good = proposal(
        [("A风险。", ["E001"]), ("A补救。", ["E002"]), ("B风险。", ["E003"]), ("B补救。", ["E004"])]
    )
    assert checked(good, cards, "请对照风险和补救。")["findings"] == []
    # A narrow remedy-only question does not acquire an invented risk requirement.
    assert checked(bad, cards, "只问补救。")["findings"] == []


def test_attaching_risk_citation_to_repair_only_text_does_not_hide_missing_risk():
    cards = [text_card("Consequence", "a crash risk"), text_card("Remedy", "free repair")]
    padded = proposal([("补救为免费更新软件。", ["E001", "E002"])])
    assert checked(padded, cards, "说明风险和补救。")["findings"] == [
        {
            "source_id": "SOURCE-A",
            "code": "requested_source_field_not_cited",
            "field": "Consequence",
        }
    ]


def test_unretrieved_risk_field_stays_a_gap_in_an_answered_recall_task():
    cards = [text_card("Summary", "a defect"), text_card("Remedy", "a repair")]
    answered = proposal([("缺陷。", ["E001"]), ("补救。", ["E002"])])
    assert checked(answered, cards, "说明缺陷、风险和补救。")["findings"] == [
        {
            "source_id": "SOURCE-A",
            "code": "requested_source_field_not_cited",
            "field": "Consequence",
        }
    ]
    answered.disposition = "insufficient_evidence"
    assert checked(answered, cards, "说明缺陷、风险和补救。")["findings"] == []


@pytest.mark.parametrize("statement", ["B明确取代A；生产日期未知。", "两份存在相似症状。"])
def test_empty_candidate_relation_cannot_prove_positive_claim(statement):
    cards = [relation_card()]
    result = checked(proposal([(statement, ["E001"])], kind="relationship"), cards)
    assert any(f["code"] == "unconfirmed_relation_asserted" for f in result["findings"])
    assert (
        checked(proposal([("相似症状关系尚未确认。", ["E001"])], kind="relationship"), cards)[
            "findings"
        ]
        == []
    )
    assert (
        checked(
            proposal([(statement, ["E001"])], kind="relationship"), [relation_card(confirmed=True)]
        )["findings"]
        == []
    )


def test_matching_body_text_must_be_cited_locally_for_symptom_details():
    cards = [relation_card(), text_card("Summary", "loss of ABS and DSC")]
    proposed = proposal([("有相似症状（ABS/DSC失效）。", ["E001"])], kind="relationship")
    assert checked(proposed, cards)["status"] == "rejected"
    proposed.claims[0].support_ids.append("E002")
    assert checked(proposed, cards)["findings"] == []


def test_insufficiency_kind_does_not_hide_a_positive_symptom_assertion():
    cards = [relation_card()]
    proposed = proposal([("相似症状（ABS失效）不证明同一缺陷。", ["E001"])], kind="insufficiency")
    assert checked(proposed, cards)["status"] == "rejected"


def test_confirmed_mention_alone_still_cannot_prove_replacement():
    card = relation_card(confirmed=True)
    card.payload["assessment"]["links"][0]["supporting_evidence"] = [
        {"quote": "A mentions identifier B"}
    ]
    proposed = proposal([("B明确取代A。", ["E001"])], kind="relationship")
    assert checked(proposed, [card])["findings"][0]["code"] == "replacement_text_citation_missing"


def test_prompt_keeps_actual_source_facets_and_empty_relation_evidence():
    cards = [relation_card(), text_card("Consequence", "a risk")]
    aliases, visible = prompt_cards(cards)
    assert visible[0]["payload"]["links"][0]["supporting_evidence"] == []
    assert visible[1]["payload"]["source_fields"] == ["Consequence"]
    checklist = answer_checklist("说明风险", cards, aliases)
    assert checklist["requested_source_fields"] == ["Consequence"]
    assert checklist["semantic_support_still_requires_review"]
    assert "reference_claim" not in str(checklist)


@pytest.mark.parametrize("reason", ["no_complaint_source_allowed", "no_allowed_complaint_source"])
def test_no_complaint_source_explains_both_missing_data_requirements(reason):
    text = bounded_stop("insufficient_evidence", reason=reason, intent="complaint_statistics")
    assert "投诉全集" in text and "暴露量分母" in text and "故障发生率" in text


def test_tool_failure_does_not_invent_absence_of_complaint_sources():
    text = bounded_stop(
        "insufficient_evidence", reason="tool_budget_exhausted", intent="complaint_statistics"
    )
    assert "未能取得" in text and "不表示投诉为零" in text
    assert "资料不含" not in text


def test_waiting_for_fields_already_answers_permanent_safety_boundary():
    text = clarification_prompt(["configurations", "production_date"], intent="applicability")
    assert "configurations" in text and "production_date" in text
    assert "完成召回补救" in text and "不能据此证明车辆永久安全" in text
    assert "永久安全" not in clarification_prompt(["accept_frozen_time"], intent="record_lookup")


@pytest.mark.parametrize("action", ["search", "expand", "count", "links", "read", "finish"])
def test_current_action_format_produces_valid_decisions_without_unused_nulls(action):
    contract = decision_format([action])
    value = deepcopy(contract["required"])
    value.update(action=action, reason="SYNTHETIC", **contract["action_arguments"][action])
    if action == "finish":
        value["proposal"] = {
            "disposition": "insufficient_evidence",
            "claims": [],
            "unresolved_fields": [],
        }
    Decision.model_validate(value)
    assert set(contract["action_arguments"]) == {action}


def test_passing_mechanical_guards_is_never_reported_as_semantic_proof():
    cards = [text_card("Summary", "some bounded fact")]
    result = checked(proposal([("SYNTHETIC。", ["E001"])]), cards)
    assert result["status"] == "passed_narrow_checks"
    assert result["semantic_support"] == "not_evaluated_by_program"

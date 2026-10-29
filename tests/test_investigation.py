"""Synthetic model/claim/review edge cases; these are not real quality measurements."""

import io
import json
from copy import deepcopy
from urllib.error import HTTPError

import pytest

from ingestion.records import content_hash
from investigation.contracts import AnswerProposal, EvidenceCard, ReferenceCase
from investigation.evidence import close_context, verify_proposal
from investigation.flow import redact
from investigation.review import blind_package, export_blind, import_blind_review
from investigation.scoring import SemanticReview, score
from models.chat_api import ChatJSONModel, ModelCallFailed, local_configuration
from parsing.parser import text_hash
from retrieval.contracts import SearchScope

H = "a" * 64


class FakeJSONModel:
    identity = {"model_id": "synthetic-json", "is_simulated": True}

    def __init__(self, mode="valid"):
        self.mode = mode
        self.calls = 0

    def generate(self, system, payload, *, max_tokens):
        self.calls += 1
        if self.mode == "transport":
            raise ModelCallFailed("synthetic_transport_failure")
        kind = (
            "relationship"
            if payload["intent"] == "relationship"
            else "count"
            if payload["intent"] == "complaint_statistics"
            else "source_fact"
        )
        ids = [c["alias"] for c in payload["evidence"]]
        proposal = {
            "disposition": "answered",
            "claims": [
                {
                    "claim_id": "C001",
                    "kind": kind,
                    "statement": "SYNTHETIC claim, not a real investigation answer.",
                    "scope": payload["scope"],
                    "support_ids": ids,
                    "counter_ids": [],
                }
            ],
            "unresolved_fields": [],
        }
        if self.mode == "unknown":
            proposal["claims"][0]["support_ids"] = ["E999"]
        if self.mode == "schema":
            proposal["uncontrolled_answer"] = "unreferenced"
        return {
            "proposal": proposal,
            "accepted_complete": self.mode != "truncated",
            "usage": None,
            "elapsed_ms": 0,
            "estimated_cost": None,
            "identity": self.identity,
        }


class Opener:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def open(self, request, timeout):
        self.calls += 1
        self.request = request
        if isinstance(self.value, Exception):
            raise self.value
        return io.BytesIO(self.value)


def response(content='{"claims":[]}', finish="stop"):
    return json.dumps(
        {
            "model": "reported",
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"total_tokens": 12},
        }
    ).encode()


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"not-json", "model_response_not_json"),
        (response(""), "model_content_empty"),
        (response("{"), "model_content_invalid_json"),
        (b"x" * 2_000_001, "model_response_too_large"),
        (HTTPError("https://example", 429, "secret-error", {}, None), "model_http_429"),
        (OSError("secret-error"), "model_transport_failed"),
    ],
    ids=["not-json", "empty", "invalid-content", "oversized", "http429", "transport"],
)
def test_adapter_failures_are_single_call_and_sanitized(raw, code):
    opener = Opener(raw)
    model = ChatJSONModel({"DEEPSEEK_API_KEY": "synthetic-secret"}, opener=opener)
    with pytest.raises(ModelCallFailed, match=code) as caught:
        model.generate("json", {})
    assert str(caught.value) == code
    assert opener.calls == 1


def test_adapter_request_protocol_and_truncation():
    opener = Opener(response(finish="length"))
    model = ChatJSONModel({"DEEPSEEK_API_KEY": "synthetic-secret"}, opener=opener)
    result = model.generate("json", {"question": "hello"})
    assert not result["accepted_complete"]
    request = json.loads(opener.request.data)
    assert request["thinking"] == {"type": "disabled"}
    assert request["response_format"] == {"type": "json_object"}
    assert result["usage"]["total_tokens"] == 12
    assert "synthetic-secret" not in json.dumps(result)
    assert result["estimated_cost"] is None


def test_missing_key_does_not_make_a_request():
    opener = Opener(response())
    with pytest.raises(ModelCallFailed, match="key_not_configured"):
        ChatJSONModel({}, opener=opener).generate("json", {})
    assert opener.calls == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.deepseek.com",
        "https://attacker.test",
        "https://api.deepseek.com@attacker.test",
        "https://api.deepseek.com?key=secret",
    ],
)
def test_unapproved_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        ChatJSONModel({"DEEPSEEK_BASE_URL": endpoint})


def test_dotenv_is_whitelisted_literal_and_environment_wins(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('DEEPSEEK_API_KEY="literal$(unsafe)"\nIGNORED=bad\nRECALL_MODEL_ID=local\n')
    monkeypatch.setenv("RECALL_MODEL_ID", "shell")
    result = local_configuration(path)
    assert result["RECALL_MODEL_ID"] == "shell"
    assert result["DEEPSEEK_API_KEY"] == "literal$(unsafe)"
    assert "IGNORED" not in result


def card(kind="text", dependencies=(), channel="matched"):
    body = {
        "evidence_id": H,
        "evidence_key": H,
        "task_id": "TEST",
        "release_id": H,
        "origin_sha256": H,
        "selector": {"path": []},
        "source_ids": ["S"],
        "source_version_ids": [H],
        "kind": kind,
        "scope_channel": channel,
        "payload": {},
        "required_context_ids": list(dependencies),
    }
    return EvidenceCard.model_validate(body)


def proposal(**changes):
    claim = {
        "claim_id": "C001",
        "kind": "source_fact",
        "statement": "synthetic",
        "scope": {},
        "support_ids": ["E001"],
        "counter_ids": [],
    } | changes
    return AnswerProposal.model_validate(
        {"disposition": "answered", "claims": [claim], "unresolved_fields": []}
    )


@pytest.mark.parametrize(
    "changes,evidence,pattern",
    [
        ({"support_ids": ["E999"]}, card(), "unknown evidence"),
        ({"scope": {"model_year": 2010}}, card(), "scope differs"),
        ({"kind": "causal"}, card(), "causal confirmation"),
        ({"kind": "individual_applicability"}, card(), "individual vehicle"),
        ({}, card(dependencies=["b" * 64]), "mandatory"),
        ({}, card(channel="pending"), "cannot confirm"),
        ({"kind": "count"}, card(), "declared kind"),
    ],
)
def test_claim_constraints(changes, evidence, pattern):
    with pytest.raises(ValueError, match=pattern):
        verify_proposal(proposal(**changes), [evidence], {"E001": H}, SearchScope())


def test_real_citation_can_still_have_semantic_overstatement():
    result = verify_proposal(
        proposal(statement="All vehicles are safe forever"), [card()], {"E001": H}, SearchScope()
    )
    assert result["semantic_support"] == "not_evaluated_by_program"


def test_context_closure_records_only_visible_dependencies_and_keeps_original():
    focus = card(dependencies=["b" * 64])
    parent = card().model_copy(update={"evidence_id": "b" * 64})
    original = proposal()
    normalized, additions = close_context(original, [focus, parent], {"E001": H, "E002": "b" * 64})
    assert original.claims[0].support_ids == ["E001"]
    assert normalized.claims[0].support_ids == ["E001", "E002"]
    assert additions == [{"claim_id": "C001", "added_context_aliases": ["E002"]}]
    assert (
        verify_proposal(normalized, [focus, parent], {"E001": H, "E002": "b" * 64}, SearchScope())[
            "status"
        ]
        == "passed"
    )


def reference_and_report():
    quote = "vehicle scope; condition and outcome"
    citations = [
        {"source_version_id": H, "unit_id": H, "start": 0, "end": len(quote), "quote": quote}
    ]
    evidence = card().model_dump(mode="json") | {"payload": {"citations": citations}}
    slots = [
        {
            "slot_id": name,
            "kind": "text",
            "alternatives": [
                {
                    "source_version_id": H,
                    "unit_id": H,
                    "start": start,
                    "end": end,
                    "quote_sha256": text_hash(quote[start:end]),
                }
            ],
        }
        for name, start, end in [("scope", 0, 13), ("condition", 15, len(quote))]
    ]
    ref = ReferenceCase.model_validate(
        {
            "case_id": "SYNTHETIC",
            "split": "development",
            "slots": slots,
            "required_claims": [
                {
                    "reference_claim_id": "R1",
                    "statement": "Joint support",
                    "evidence_sets": [["scope", "condition"]],
                }
            ],
            "acceptable_dispositions": ["answered"],
        }
    )
    report = {
        "execution_status": "completed",
        "evidence_verification": {"status": "verified"},
        "claim_verification": {"status": "passed"},
        "cards": [evidence],
        "aliases": {"E001": H},
        "proposal": proposal().model_dump(mode="json"),
        "run_id": "PRIVATE-RUN",
        "request": {"question": "Question"},
        "scope": {},
        "time": {},
        "allowed_source_ids": ["S"],
        "method": "PRIVATE-METHOD",
        "model_identity": {"model_id": "PRIVATE-MODEL"},
    }
    return ref, report


def review_for(ref, report, kind="implementation_agent", **changes):
    value = {
        "report_sha256": content_hash(report),
        "reference_sha256": content_hash(ref.model_dump(mode="json")),
        "reviewer_kind": kind,
        "reviewer_id": "synthetic-reviewer",
        "claims": [
            {
                "claim_id": "C001",
                "verdict": "supported",
                "reference_claim_ids": ["R1"],
                "scope_correct": True,
                "counterevidence_handled": True,
                "reason": "synthetic",
            }
        ],
        "disposition_correct": True,
        "complete": True,
        "notes": "synthetic exercise",
    } | changes
    return SemanticReview.model_validate(value)


def test_or_and_equivalent_parent_range_and_no_fake_semantic_score():
    ref, report = reference_and_report()
    result = score(report, ref)
    assert result["machine"]["claim_evidence_coverage"]["C001"]["evidence_sets_complete_for"] == [
        "R1"
    ]
    assert result["formal_semantic_metrics"] is None
    assert result["provisional_semantic_metrics"] is None
    partial = deepcopy(report)
    partial["cards"][0]["payload"]["citations"][0].update(end=13, quote="vehicle scope")
    assert (
        score(partial, ref)["machine"]["claim_evidence_coverage"]["C001"][
            "evidence_sets_complete_for"
        ]
        == []
    )
    alternate = ref.model_copy(deep=True)
    alternate.required_claims[0].evidence_sets.append(["scope"])
    assert score(partial, alternate)["machine"]["claim_evidence_coverage"]["C001"][
        "evidence_sets_complete_for"
    ] == ["R1"]


def test_review_provenance_complete_and_dispute_gates():
    ref, report = reference_and_report()
    provisional = score(report, ref, review_for(ref, report))
    assert provisional["formal_semantic_metrics"] is None
    assert provisional["provisional_semantic_metrics"]["required_claim_recall"] == 1
    human = score(report, ref, review_for(ref, report, "human"))
    assert human["formal_semantic_metrics"]["grounded_claim_fraction"] == 1
    pending = score(report, ref, review_for(ref, report, "human", dispute_status="pending"))
    assert pending["formal_semantic_metrics"] is None
    with pytest.raises(ValueError, match="bound"):
        score(report, ref, review_for(ref, report, report_sha256="b" * 64))


def test_unseen_valid_evidence_requires_explicit_review_decision():
    ref, report = reference_and_report()
    report["cards"][0]["payload"]["citations"][0]["source_version_id"] = "b" * 64
    result = score(report, ref)
    assert result["novel_evidence_pending"] == [H]
    assert result["formal_semantic_metrics"] is None
    review = review_for(
        ref,
        report,
        novel_evidence_decisions=[
            {
                "reference_claim_id": "R1",
                "evidence_ids": [H],
                "accepted": True,
                "reason": "synthetic equivalent evidence",
            }
        ],
    )
    assert score(report, ref, review)["provisional_semantic_metrics"]["required_claim_recall"] == 1


def test_blind_export_hides_method_and_roundtrips_judgment(tmp_path):
    ref, report = reference_and_report()
    public, private = blind_package(report)
    serialized = json.dumps(public)
    assert all(s not in serialized for s in ["PRIVATE-METHOD", "PRIVATE-MODEL", "PRIVATE-RUN"])
    review = review_for(ref, report).model_dump(
        mode="json", exclude={"report_sha256", "reference_sha256"}
    )
    imported = import_blind_review(
        review | {"blind_id": public["blind_id"]},
        private,
        content_hash(ref.model_dump(mode="json")),
    )
    assert imported.report_sha256 == content_hash(report)
    identifier = export_blind(report, tmp_path)
    assert (tmp_path / (identifier + ".html")).is_file()


def test_prompt_minimizes_identifiers():
    result = redact("VIN 1HGCM82633A004352 email name@example.com phone +1 (555) 123-4567")
    assert "1HGCM82633A004352" not in result
    assert "name@example.com" not in result
    assert "123-4567" not in result
    assert redact("2009-01-01") == "2009-01-01"


def test_structured_tuple_fact_survives_json_artifact_roundtrip(tmp_path):
    from types import SimpleNamespace

    from investigation.evidence import structured_card, verify_cards
    from runtime.artifacts import ArtifactStore

    payload = {"task_id": "TUPLE", "release_id": H, "matched_vehicle": [("FORD", "F-150", 2016)]}
    context = SimpleNamespace(
        task={"task_id": "TUPLE"},
        manifest={"release_id": H},
        sources=["S"],
        records={H: None},
        service=SimpleNamespace(count=lambda *args: payload),
    )
    store = ArtifactStore(tmp_path)
    origin = store.put_json(
        {
            "tool": "count",
            "input": {
                "request": {"task_id": "TUPLE", "query": "count", "expected_release_id": H},
                "components": [],
            },
            "output": payload,
        }
    )["sha256"]
    evidence = structured_card(context, origin, payload, "count")
    assert evidence.payload["matched_vehicle"] == [["FORD", "F-150", 2016]]
    assert verify_cards(context, store, [evidence])["status"] == "verified"


def test_context_card_retains_its_own_scope_dependencies():
    from types import SimpleNamespace

    from investigation.evidence import text_cards

    context = SimpleNamespace(
        task={"task_id": "TEST"},
        manifest={"release_id": H},
        documents={"DOC": {"metadata": {"source_id": "S", "source_version_id": H}}},
    )
    scope = {"chunk_id": "scope", "citations": [], "text": "scope", "required_context_ids": []}
    section = {
        "chunk_id": "section",
        "citations": [],
        "text": "section",
        "required_context_ids": ["scope"],
    }
    focus = {
        "chunk_id": "focus",
        "citations": [],
        "text": "focus",
        "required_context_ids": ["section"],
    }
    output = {
        "focus": {
            "document_id": "DOC",
            "scope_channel": "matched",
            "bundle": {"context": [scope, section], "focus": focus},
        },
        "related": [],
    }
    cards = text_cards(context, H, output)
    assert cards[1].required_context_ids == [cards[0].evidence_id]
    assert set(cards[2].required_context_ids) == {cards[0].evidence_id, cards[1].evidence_id}
    aliases = {
        "E001": cards[0].evidence_id,
        "E002": cards[1].evidence_id,
        "E003": cards[2].evidence_id,
    }
    normalized, _ = close_context(proposal(support_ids=["E002"]), cards, aliases)
    assert normalized.claims[0].support_ids == ["E002", "E001"]

"""Synthetic review-contract fixtures; never counted as real human judgments."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from reviewing.contracts import Citation, Review
from reviewing.policy import POLICY
from reviewing.scoring import assess, context_links


def fixture():
    reference = ReferenceCase.model_validate(
        {
            "case_id": "SYNTHETIC",
            "split": "test",
            "slots": [
                {
                    "slot_id": "S1",
                    "kind": "structured_lookup",
                    "expected_fields": {"counts": {"matched": 1}},
                }
            ],
            "required_claims": [
                {"reference_claim_id": "R1", "statement": "A B", "evidence_sets": [["S1"]]}
            ],
            "acceptable_dispositions": ["insufficient_evidence"],
        }
    )
    case = SimpleNamespace(
        case_id="SYNTHETIC",
        reference_sha256=content_hash(reference.model_dump(mode="json")),
        category="direct",
    )
    report = {
        "execution_status": "completed",
        "evidence_verification": {"status": "verified"},
        "claim_verification": {"status": "passed"},
        "cards": [
            {
                "evidence_id": "a" * 64,
                "kind": "structured_lookup",
                "payload": {"counts": {"matched": 1}},
                "required_context_ids": ["b" * 64],
            },
            {
                "evidence_id": "b" * 64,
                "kind": "text",
                "payload": {"text": "scope", "citations": []},
                "required_context_ids": [],
            },
        ],
        "aliases": {"E001": "a" * 64, "E002": "b" * 64},
        "proposal": {
            "claims": [
                {
                    "claim_id": "C001",
                    "statement": "A B",
                    "support_ids": ["E001", "E002"],
                    "counter_ids": [],
                }
            ]
        },
    }
    observation = {
        "cell": {"cell_id": "SYNTHETIC:1", "case_id": "SYNTHETIC"},
        "blind_id": "BLIND-SYNTHETIC",
        "state": "completed",
        "report_content_sha256": content_hash(report),
        "report_reference": {"SYNTHETIC": True},
    }
    source = {"reviewer_kind": "synthetic", "note": "SYNTHETIC fixture only"}
    review = {
        "policy_sha256": content_hash(POLICY),
        "experiment_id": "f" * 64,
        "cell_id": "SYNTHETIC:1",
        "blind_id": "BLIND-SYNTHETIC",
        "observation_sha256": content_hash(observation),
        "report_sha256": content_hash(report),
        "reference_sha256": case.reference_sha256,
        "reviewer_kind": "synthetic",
        "reviewer_id": "SYNTHETIC",
        "source_kind": "synthetic_fixture",
        "sources": [{"name": "fixture", "content_sha256": content_hash(source)}],
        "transcribed_by": "SYNTHETIC",
        "complete_task_reading": True,
        "task_success": False,
        "disposition_correct": True,
        "claims": [
            {
                "claim_id": "C001",
                "verdict": "partial",
                "scope_verdict": "correct",
                "reason": "SYNTHETIC",
            }
        ],
        "citations": [
            {
                "claim_id": "C001",
                "evidence_alias": "E001",
                "role": "direct",
                "direct_verdict": "supported",
                "evaluated_aspect": "A",
                "reason": "SYNTHETIC",
            },
            {
                "claim_id": "C001",
                "evidence_alias": "E002",
                "role": "required_context",
                "reason": "SYNTHETIC scope",
            },
        ],
        "assertions": [
            {
                "assertion_id": "A1",
                "claim_id": "C001",
                "start": 0,
                "end": 1,
                "text": "A",
                "verdict": "supported",
                "reason": "SYNTHETIC",
            },
            {
                "assertion_id": "A2",
                "claim_id": "C001",
                "start": 2,
                "end": 3,
                "text": "B",
                "verdict": "partial",
                "reason": "SYNTHETIC",
            },
        ],
        "assertions_exhaustive": True,
        "notes": "SYNTHETIC unit-test fixture, not actual human review",
    }
    return observation, report, reference, case, review, {"fixture": source}


def run(parts):
    observation, report, reference, case, review, sources = parts
    return assess(
        observation,
        report,
        reference,
        case,
        review,
        experiment_id="f" * 64,
        sources=sources,
        inputs={},
    )


def rebind(parts):
    obs, report, _, _, review, _ = parts
    obs["report_content_sha256"] = content_hash(report)
    review["report_sha256"] = content_hash(report)
    review["observation_sha256"] = content_hash(obs)


def test_context_role_does_not_inflate_direct_or_unsupported_rates():
    result = run(fixture())
    assert result["formal"] is None
    metrics = result["provisional"]
    assert metrics["direct_citation_pairs"] == 1
    assert metrics["context_only_citation_pairs"] == 1
    assert metrics["direct_citation_aspect_support_rate"] == 1
    assert metrics["required_context_coverage"] == 1
    assert metrics["partial_atomic_assertion_rate"] == 0.5
    assert metrics["unsupported_atomic_assertion_rate"] == 0
    assert metrics["task_success"] is False


@pytest.mark.parametrize(
    "change",
    ["missing_citation", "unresolved_role", "not_evaluable", "missing_assertion", "dispute"],
)
def test_missing_components_and_dispute_remain_unknown(change):
    p = fixture()
    review = p[4]
    if change == "missing_citation":
        review["citations"].pop()
    elif change == "unresolved_role":
        review["citations"][1]["role"] = "unresolved"
    elif change == "not_evaluable":
        review["citations"][0]["direct_verdict"] = "not_evaluable"
    elif change == "missing_assertion":
        review["assertions"] = []
        review["assertions_exhaustive"] = False
    else:
        review["dispute_status"] = "pending"
    m = run(p)["provisional"]
    if change == "missing_assertion":
        assert m["unsupported_atomic_assertion_rate"] is None
    else:
        assert m["direct_citation_aspect_support_rate"] is None
    if change == "dispute":
        assert m["task_success"] is None


@pytest.mark.parametrize(
    "change",
    ["report", "observation", "reference", "policy", "cell", "source", "source_identity", "input"],
)
def test_exact_bindings_reject_changed_or_forged_reviews(change):
    p = fixture()
    if change == "report":
        p[1]["proposal"]["claims"][0]["statement"] = "changed"
    elif change == "observation":
        p[0]["state"] = "failed"
    elif change == "reference":
        p[4]["reference_sha256"] = "c" * 64
    elif change == "policy":
        p[4]["policy_sha256"] = "c" * 64
    elif change == "cell":
        p[4]["cell_id"] = "OTHER"
    elif change == "source":
        p[5]["fixture"]["note"] = "changed"
    elif change == "source_identity":
        p[4]["reviewer_kind"] = "human"
        p[4]["source_kind"] = "actual_human_reply"
    else:
        p[4]["input_visibility"] = {
            "input_artifact_sha256": "d" * 64,
            "input_content_sha256": "e" * 64,
            "check": "exact_input_checked",
            "note": "SYNTHETIC not a bound input",
        }
    with pytest.raises(ValueError):
        run(p)


def test_context_role_laundering_duplicate_and_self_declared_human_are_rejected():
    p = fixture()
    p[4]["citations"][0] = {
        "claim_id": "C001",
        "evidence_alias": "E001",
        "role": "required_context",
        "reason": "SYNTHETIC denominator laundering",
    }
    with pytest.raises(ValueError, match="actual_required_context"):
        run(p)
    p = fixture()
    p[4]["citations"].append(deepcopy(p[4]["citations"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        run(p)
    p = fixture()
    p[4]["reviewer_kind"] = "human"
    with pytest.raises(ValueError, match="conflicts"):
        Review.model_validate(p[4])
    with pytest.raises(ValueError):
        Citation(
            claim_id="C001",
            evidence_alias="E002",
            role="required_context",
            direct_verdict="supported",
            reason="SYNTHETIC",
        )


def test_missing_context_and_cycles_are_detected_independently_of_judgments():
    p = fixture()
    p[1]["proposal"]["claims"][0]["support_ids"] = ["E001"]
    p[4]["citations"].pop()
    rebind(p)
    assert run(p)["provisional"]["required_context_coverage"] == 0
    p[1]["cards"][1]["required_context_ids"] = ["a" * 64]
    with pytest.raises(ValueError, match="cyclic"):
        context_links(p[1])


@pytest.mark.parametrize("change", ["incomplete", "overlap", "wrong_text", "out_of_bounds"])
def test_atomic_assertions_require_exact_nonoverlapping_complete_spans(change):
    p = fixture()
    if change == "incomplete":
        p[4]["assertions"].pop()
    elif change == "overlap":
        a = deepcopy(p[4]["assertions"][0])
        a["assertion_id"] = "A3"
        p[4]["assertions"].append(a)
    elif change == "wrong_text":
        p[4]["assertions"][0]["text"] = "other"
    else:
        p[4]["assertions"][1]["end"] = 100
    with pytest.raises(ValueError):
        run(p)


def positive_fixture():
    p = fixture()
    p[4]["task_success"] = True
    p[4]["claims"][0]["verdict"] = "supported"
    p[4]["assertions"][1]["verdict"] = "supported"
    p[4]["obligations"] = [
        {
            "reference_claim_id": "R1",
            "verdict": "satisfied",
            "claim_ids": ["C001"],
            "support_ids": ["E001"],
            "reason": "SYNTHETIC",
        }
    ]
    return p


def test_positive_task_requires_obligations_and_grounding_not_just_citation_rate():
    assert run(positive_fixture())["provisional"]["task_success"] is True
    p = positive_fixture()
    p[4]["obligations"] = []
    with pytest.raises(ValueError, match="positive_task"):
        run(p)
    p = positive_fixture()
    p[4]["obligations"][0]["support_ids"] = ["E002"]
    with pytest.raises(ValueError, match="OR_of_AND"):
        run(p)
    p = positive_fixture()
    p[1]["evidence_verification"]["status"] = "failed"
    rebind(p)
    with pytest.raises(ValueError, match="positive_task"):
        run(p)


def test_wrong_relation_positive_requires_scope_and_unknown_review():
    p = positive_fixture()
    p[3].category = "wrong_relation"
    with pytest.raises(ValueError, match="positive_task"):
        run(p)
    p[4]["relations"] = [
        {"relation_type": name, "verdict": "correctly_unknown", "reason": "SYNTHETIC"}
        for name in ["excluded_by_scope", "applicability_unknown"]
    ]
    assert run(p)["provisional"]["task_success"] is True


def test_counterevidence_has_separate_role_and_metric():
    p = fixture()
    p[1]["proposal"]["claims"][0]["counter_ids"] = ["E002"]
    p[1]["proposal"]["claims"][0]["support_ids"] = ["E001"]
    p[4]["citations"][1] |= {
        "role": "counterevidence",
        "counter_verdict": "supported",
        "evaluated_aspect": "counterfact",
    }
    rebind(p)
    assert run(p)["provisional"]["direct_citation_pairs"] == 1
    p[4]["citations"][1].pop("counter_verdict")
    p[4]["citations"][1] |= {"role": "direct", "direct_verdict": "supported"}
    with pytest.raises(ValueError, match="counter_only"):
        run(p)


def test_incomplete_template_and_negative_review_do_not_invent_remaining_metrics():
    p = fixture()
    p[4]["complete_task_reading"] = False
    assert run(p)["provisional"]["task_success"] is None
    p = fixture()
    p[4]["assertions"] = []
    p[4]["assertions_exhaustive"] = False
    m = run(p)["provisional"]
    assert m["task_success"] is False
    assert m["unsupported_atomic_assertion_rate"] is None
    assert m["required_obligation_recall"] is None


def human_style_fixture():
    """Synthetic representation of human metadata; not actual human participation."""
    p = fixture()
    source = {
        "reviewer_kind": "human",
        "reviewer_id": "SYNTHETIC",
        "observation_sha256": p[4]["observation_sha256"],
        "report_content_sha256": p[4]["report_sha256"],
        "reference_sha256": p[4]["reference_sha256"],
        "complete_task_reading": True,
        "task_success": False,
        "disposition_correct": True,
        "claim_verdicts": {"C001": "partial"},
    }
    p[5]["fixture"] = source
    p[4]["sources"][0]["content_sha256"] = content_hash(source)
    p[4]["reviewer_kind"] = "human"
    p[4]["source_kind"] = "actual_human_reply"
    return p


@pytest.mark.parametrize("field", ["task_success", "disposition_correct", "claim_verdict"])
def test_synthetic_human_style_import_cannot_change_recorded_judgment(field):
    p = human_style_fixture()
    assert run(p)["formal"]["task_success"] is False
    if field == "claim_verdict":
        p[4]["claims"][0]["verdict"] = "supported"
    else:
        p[4][field] = not p[4][field]
    with pytest.raises(ValueError, match="trusted_replies"):
        run(p)


def test_aggregate_keeps_unreviewed_and_unstarted_denominator_and_avoids_success_only():
    from reviewing.corpus import arm_metrics

    m = run(human_style_fixture())["formal"]
    reviewed = {"human_task_judgment": False, "formal": m}
    missing = {"human_task_judgment": None, "formal": None}
    result = arm_metrics([reviewed, missing])
    assert result["denominator"] == 2 and result["human_task_judgments"] == 1
    assert result["task_completion_rate"] is None
    assert result["direct_citation_aspect_support_rate"] is None
    complete = arm_metrics([reviewed])
    assert complete["task_completion_rate"] == 0
    assert complete["direct_citation_aspect_support_rate"] == 1


def test_blind_packet_shows_actual_minimized_text_without_method_or_call_count():
    from reviewing.corpus import public_packet

    _, report, _, case, _, _ = fixture()
    report = deepcopy(report)
    case.request = SimpleNamespace(question="SYNTHETIC")
    report |= {
        "request": {"question": "SYNTHETIC question"},
        "scope": {},
        "time": {},
        "allowed_source_ids": ["SYNTHETIC"],
        "run_id": "HIDDEN",
        "method": "HIDDEN",
    }
    for card in report["cards"]:
        card |= {"scope_channel": "matched", "source_ids": ["SYNTHETIC"]}
    report["cards"][1]["payload"]["text"] = "Call 1-800-853-1403."
    packet = public_packet(
        {
            "observation": {"blind_id": "BLIND-SYNTHETIC"},
            "report": report,
            "case": case,
            "inputs": {
                "d" * 64: {
                    "payload": {
                        "evidence": [
                            {
                                "payload": {
                                    "source_ids": ["SYNTHETIC"],
                                    "text": "Call [NUMBER_REDACTED].",
                                }
                            }
                        ]
                    }
                }
            },
        },
        "ANALYSIS",
    )
    assert packet["evidence"][1]["input_visibility"]["status"] == "exact_minimized_text_seen"
    assert packet["evidence"][1]["input_visibility"]["text_differs_from_raw_source"] is True
    serialized = str(packet)
    assert "HIDDEN" not in serialized and "recorded_provider_inputs" not in serialized


def corpus_fixture(tmp_path, monkeypatch):
    from reviewing import corpus

    p = human_style_fixture()
    p[0]["cell"]["arm_id"] = "fixed"
    p[1].update(
        {
            "request": {"question": "SYNTHETIC"},
            "scope": {},
            "time": {},
            "allowed_source_ids": ["SYNTHETIC"],
            "run_id": "HIDDEN",
        }
    )
    for card in p[1]["cards"]:
        card |= {"scope_channel": "matched", "source_ids": ["SYNTHETIC"]}
    rebind(p)
    p[5]["fixture"] |= {
        "observation_sha256": p[4]["observation_sha256"],
        "report_content_sha256": p[4]["report_sha256"],
    }
    source_path = tmp_path / "SYNTHETIC-source.json"
    source_path.write_text(json.dumps(p[5]["fixture"]), encoding="utf-8")
    p[4]["sources"] = [
        {"name": source_path.as_posix(), "content_sha256": content_hash(p[5]["fixture"])}
    ]
    row = {
        "cell": p[0]["cell"],
        "state": "completed",
        "case": p[3],
        "reference": p[2],
        "observation": p[0],
        "report": p[1],
        "inputs": {},
    }
    identity = {"experiment_id": "f" * 64}
    protocol = SimpleNamespace(arms=[SimpleNamespace(arm_id="fixed")])
    index = {"rows": {"SYNTHETIC:1": {"state": "completed"}}}
    monkeypatch.setattr(corpus, "load_cells", lambda *a, **kw: (identity, protocol, index, [row]))
    pointer = corpus.prepare(
        tmp_path / "SYNTHETIC-identity.json", tmp_path / "analysis", [source_path]
    )
    return corpus, pointer, p[4], source_path


def test_review_corpus_seal_idempotency_import_and_conflict_never_overwrite(tmp_path, monkeypatch):
    corpus, pointer, value, source_path = corpus_fixture(tmp_path, monkeypatch)
    again = corpus.prepare(
        tmp_path / "SYNTHETIC-identity.json", tmp_path / "analysis", [source_path]
    )
    assert pointer == again
    assert corpus.import_review(pointer, value)["formal"]["task_success"] is False
    corpus.import_review(pointer, value)
    summary = corpus.analyze(pointer)
    assert summary["human_task_judgments"] == 1 and summary["rows"][0]["imported_reviews"] == 1
    second = deepcopy(value)
    second["notes"] = "SYNTHETIC second source-consistent review version, requires adjudication"
    corpus.import_review(pointer, second)
    summary = corpus.analyze(pointer)
    assert summary["rows"][0]["human_review_conflict"] is True
    assert summary["human_task_judgments"] == 0
    assert summary["arms"]["fixed"]["task_completion_rate"] is None


@pytest.mark.parametrize(
    "mutation", ["code", "archive", "registry", "observation", "review", "index"]
)
def test_corpus_changes_are_refused_and_unreviewed_templates_are_not_human(
    tmp_path, monkeypatch, mutation
):
    from pathlib import Path

    from runtime.artifacts import native_path

    corpus, pointer, value, _ = corpus_fixture(tmp_path, monkeypatch)
    assert corpus.analyze(pointer)["human_task_judgments"] == 0
    corpus.import_review(pointer, value)
    directory = Path(pointer["directory"])
    if mutation == "code":
        monkeypatch.setattr(corpus, "implementation", lambda: {"SYNTHETIC": "changed"})
    elif mutation == "archive":
        path = next((directory / "analysis-source" / "reviewing").glob("*.py"))
        native_path(path).write_text("changed", encoding="utf-8")
    elif mutation == "registry":
        registry = corpus.read(directory / "source-registry.json")
        next(iter(registry.values()))["task_success"] = True
        native_path(directory / "source-registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
    elif mutation == "observation":
        original = corpus.load_cells

        def changed(*a, **kw):
            identity, protocol, index, rows = deepcopy(original(*a, **kw))
            rows[0]["observation"]["state"] = "failed"
            return identity, protocol, index, rows

        monkeypatch.setattr(corpus, "load_cells", changed)
    elif mutation == "review":
        path = next((directory / "reviews").glob("*.json"))
        record = corpus.read(path)
        record["result"]["formal"]["task_success"] = True
        native_path(path).write_text(json.dumps(record), encoding="utf-8")
    else:
        index = corpus.read(directory / "review-index.json")
        index["UNREGISTERED"] = next(iter(index.values()))
        native_path(directory / "review-index.json").write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError):
        corpus.analyze(pointer)


def test_structured_visibility_distinguishes_query_counts_from_hidden_row_reasons():
    from reviewing.corpus import public_packet

    _, report, _, case, _, _ = fixture()
    report = deepcopy(report)
    report |= {
        "request": {"question": "SYNTHETIC"},
        "scope": {},
        "time": {},
        "allowed_source_ids": ["SYNTHETIC"],
        "run_id": "HIDDEN",
    }
    for card in report["cards"]:
        card |= {"scope_channel": "matched", "source_ids": ["SYNTHETIC"]}
    report["cards"][0]["payload"] |= {
        "source_ids": ["SYNTHETIC"],
        "rows": [{"reasons": ["model_unknown"]}],
    }
    row = {
        "observation": {"blind_id": "BLIND-SYNTHETIC"},
        "report": report,
        "case": case,
        "inputs": {
            "d" * 64: {
                "payload": {
                    "evidence": [
                        {"payload": {"source_ids": ["SYNTHETIC"], "counts": {"matched": 1}}}
                    ]
                }
            }
        },
    }
    card = public_packet(row, "ANALYSIS")["evidence"][0]
    assert card["input_visibility"]["status"] == "exact_minimized_structured_payload_seen"
    assert card["input_visibility"]["raw_fields_not_in_matched_payload"] == ["rows"]
    assert "model_unknown" not in str(card["input_visibility"]["model_payload"])
    row["inputs"] = {}
    assert (
        public_packet(row, "ANALYSIS")["evidence"][0]["input_visibility"]["status"]
        == "not_resolved_by_exact_match"
    )

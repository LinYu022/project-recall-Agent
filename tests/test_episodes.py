"""Synthetic scope/projection/protocol checks, not history quality measurements."""

import pytest
from pydantic import ValidationError

from control.contracts import Plan
from investigation.agent_contracts import Decision
from memory.episode_contracts import HistoryQuery, Window, compatible_scope
from memory.episode_projection import report_events, summary
from memory.episode_runtime import augment_history
from retrieval.contracts import SearchScope
from tests.test_single_agent import decision


@pytest.mark.parametrize("year", [2011, None])
def test_similar_model_without_matching_known_year_is_ineligible(year):
    current = SearchScope(make="TOYOTA", model="PRIUS", model_year=2010)
    assert not compatible_scope(current, SearchScope(make="TOYOTA", model="PRIUS", model_year=year))
    assert compatible_scope(current, SearchScope(make="toyota", model="prius", model_year=2010))


@pytest.mark.parametrize(
    "change",
    [
        {"owner_id": "Other"},
        {"limit": 200},
        {"allow_cross_release": "true"},
        {"before_episode_id": ""},
    ],
)
def test_history_query_cannot_supply_identity_or_unbounded_work(change):
    with pytest.raises(ValidationError):
        HistoryQuery.model_validate(
            {"project_id": "recall", "release_id": "a" * 64, "scope": {}, "allowed_source_ids": []}
            | change
        )


def test_history_window_is_bounded_and_requires_an_anchor():
    with pytest.raises(ValidationError):
        Window(
            project_id="recall",
            release_id="a" * 64,
            scope={},
            allowed_source_ids=[],
            episode_id="EP-a",
            event_id="HE-a",
            radius=10,
        )


@pytest.mark.parametrize(
    "value",
    [
        decision("history_search"),
        decision("history_window"),
        decision("search", query="brake", history_episode_id="forged"),
    ],
)
def test_history_decisions_are_separate_typed_navigation(value):
    with pytest.raises(ValidationError):
        Decision.model_validate(value)


def test_history_cannot_switch_projects_via_memory_or_inherit_another_namespace():
    with pytest.raises(ValidationError):
        Plan.model_validate(
            {
                "release_id": "a" * 64,
                "allowed_source_ids": ["x"],
                "scope": {},
                "request": {
                    "task_id": "X",
                    "question": "x",
                    "retrieval_query": "x",
                    "intent": "record_lookup",
                },
                "project_id": "one",
                "history": {"project_id": "two"},
            }
        )


def test_projection_retains_negatives_and_failures_but_not_final_answer_or_claim_text():
    report = {
        "execution_status": "failed",
        "answer": "SECRET-OLD-FINAL-ANSWER",
        "proposal": {"disposition": "answered", "claims": [{"statement": "SECRET-OLD-CLAIM"}]},
        "aliases": {"E001": "a" * 64},
        "recorded_counterevidence_aliases": ["E001"],
        "planner_revisions": [
            {
                "hypotheses": [
                    {
                        "hypothesis_id": "H001",
                        "statement": "SYNTHETIC disproven analogy",
                        "status": "contradicted",
                        "counter_ids": ["E001"],
                    }
                ]
            }
        ],
        "error_code": "SYNTHETIC-timeout",
    }
    body = {
        "task_id": "OLD",
        "task_state": "FAILED",
        "scope": {},
        "release_id": "b" * 64,
        "time": {},
        "allowed_source_ids": [],
        "question": "SYNTHETIC brake",
        "query": "brake",
        "events": report_events(report),
    }
    projected = summary(body)
    assert projected["negative_hypotheses"] and projected["counterevidence_count"] == 1
    assert projected["failure_labels"] == ["SYNTHETIC-timeout"]
    assert "SECRET-OLD" not in str(projected) and "E001" not in str(projected)


def test_history_default_off_preserves_original_provider_input():
    payload = {"question": "SYNTHETIC", "evidence": []}
    system, value = augment_history("unchanged", payload)
    assert system == "unchanged" and value is payload

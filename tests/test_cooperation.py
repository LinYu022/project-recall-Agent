"""Synthetic policy/schema/conflict checks; no real model-quality assertions."""

import pytest
from pydantic import ValidationError

from investigation.agent_contracts import Decision
from investigation.collaboration_contracts import Assessment, BranchResult, Delegation
from investigation.contracts import FixedConfig, InvestigationRequest
from investigation.cooperation import allocations, conflicts, trigger
from tests.test_single_agent import decision


def delegation():
    return {
        "delegation_id": "DATA",
        "role": "data",
        "parent_task_id": "PARENT",
        "revision_id": "REV",
        "authorization_id": "AUTH",
        "epoch": 1,
        "release_id": "a" * 64,
        "manifest_sha256": "b" * 64,
        "goal": "SYNTHETIC bounded goal",
        "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
        "time": {"mode": "snapshot_analysis"},
        "allowed_source_ids": ["SYNTHETIC-SOURCE"],
        "allowed_tools": ["lookup", "links", "finish"],
        "allocation": {
            "model_calls": 2,
            "tool_calls": 2,
            "prompt_characters": 16000,
            "completion_tokens": 1000,
        },
        "completion_conditions": ["SYNTHETIC preserve unknowns"],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_tools": ["lookup", "search", "finish"]},
        {"role": "document"},
        {"authorization": {"approved": True}},
        {"epoch": 0},
        {
            "allocation": {
                "model_calls": 100,
                "tool_calls": 2,
                "prompt_characters": 16000,
                "completion_tokens": 1000,
            }
        },
    ],
)
def test_delegation_cannot_enlarge_role_or_create_an_authorization_channel(change):
    with pytest.raises(ValidationError):
        Delegation.model_validate(delegation() | change)


def test_policy_uses_only_current_task_features_and_can_be_disabled():
    request = InvestigationRequest(
        task_id="SYNTHETIC", question="SYNTHETIC", retrieval_query="brake", intent="cross_source"
    )
    cfg = FixedConfig(max_model_calls=8, max_tool_calls=10)
    assert trigger(request, 2, cfg)["delegate"]
    assert not trigger(request.model_copy(update={"intent": "record_lookup"}), 1, cfg)["delegate"]
    assert not trigger(request, 2, cfg.model_copy(update={"multi_agent_enabled": False}))[
        "delegate"
    ]
    assert not trigger(request, 2, FixedConfig())["delegate"]
    assert not trigger(request, 2, cfg.model_copy(update={"total_prompt_characters": 15000}))[
        "delegate"
    ]


def test_branch_allocations_share_one_envelope_instead_of_copying_it():
    cfg = FixedConfig(max_model_calls=8, max_tool_calls=10)
    values = allocations(cfg)
    for field, total in [
        ("model_calls", 8),
        ("tool_calls", 10),
        ("prompt_characters", 96000),
        ("completion_tokens", 4000),
    ]:
        assert sum(getattr(v, field) for v in values.values()) == total
    assert max(v.model_calls for v in values.values()) < cfg.max_model_calls


def test_structured_conflict_is_unresolved_regardless_of_repeated_support_votes():
    positive = Assessment(
        issue="applicability",
        subject="TASK",
        value="supported",
        support_ids=["E001"],
        reason="SYNTHETIC proposal",
    )
    negative = positive.model_copy(update={"value": "contradicted"})
    data = BranchResult(delegation_id="DATA", status="completed", assessments=[positive] * 5)
    document = BranchResult(delegation_id="DOCUMENT", status="completed", assessments=[negative])
    result = conflicts([data, document])
    assert len(result) == 1 and result[0]["status"].startswith("unresolved")
    assert len(result[0]["proposals"]) == 2


@pytest.mark.parametrize(
    "steps",
    [
        [{"action": "expand", "document_id": "D001"}],
        [{"action": "search"}, {"action": "links"}],
        [{"action": "exec_shell"}, {"action": "links"}],
    ],
)
def test_parallel_batches_are_typed_and_bounded(steps):
    with pytest.raises(ValidationError):
        Decision.model_validate(decision("batch", batch=steps))

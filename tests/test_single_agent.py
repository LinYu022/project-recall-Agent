"""Scripted synthetic decisions and resource boundaries, never real agent quality labels."""

import pytest
from pydantic import ValidationError

from investigation.agent_contracts import Decision
from investigation.contracts import FixedConfig
from investigation.resources import RunBudget
from models.chat_api import ModelCallFailed


def decision(action, **changes):
    return {
        "action": action,
        "reason": "SYNTHETIC check an evidence gap",
        "hypotheses": [],
        "evidence_gaps": ["unverified source condition"],
        "next_steps": ["inspect evidence"],
        "query": None,
        "document_id": None,
        "proposal": None,
    } | changes


def finish(payload, *, counter=False):
    aliases = [
        e["alias"]
        for e in payload["evidence"]
        if e["kind"] == "text" and e["scope_channel"] == "matched"
    ]
    kind = "source_fact"
    if payload["intent"] == "complaint_statistics":
        aliases = [e["alias"] for e in payload["evidence"] if e["kind"] == "count"]
        kind = "count"
    proposal = {
        "disposition": "answered" if aliases else "insufficient_evidence",
        "claims": [
            {
                "claim_id": "C001",
                "kind": kind,
                "statement": "SYNTHETIC bounded answer, not a real quality result",
                "scope": payload["scope"],
                "support_ids": aliases,
                "counter_ids": aliases[:1] if counter else [],
            }
        ]
        if aliases
        else [],
        "unresolved_fields": [] if aliases else ["source_coverage"],
    }
    return decision("finish", proposal=proposal)


class ScriptedAgent:
    identity = {"model_id": "scripted-single-agent", "is_simulated": True}

    def __init__(self, steps):
        self.steps = steps
        self.calls = 0
        self.inputs = []

    def generate(self, system, payload, *, max_tokens):
        self.inputs.append(payload)
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        value = step(payload) if callable(step) else step
        return {
            "identity": self.identity,
            "proposal": value,
            "accepted_complete": True,
            "usage": {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40},
            "usage_is_synthetic": True,
            "elapsed_ms": 0,
            "estimated_cost": None,
        }


@pytest.mark.parametrize(
    "value",
    [
        decision("search"),
        decision("expand"),
        decision("count", query="arbitrary"),
        decision("finish"),
        decision(
            "search",
            query="brake",
            proposal={
                "disposition": "insufficient_evidence",
                "claims": [],
                "unresolved_fields": [],
            },
        ),
        decision("expand", document_id="NOT-SEEN"),
        decision("exec_shell"),
        decision("count") | {"authorization": {"approved": True}},
    ],
)
def test_typed_decisions_reject_uncontrolled_actions_and_mixed_answers(value):
    with pytest.raises(ValidationError):
        Decision.model_validate(value)


def test_resource_reservations_use_actual_returned_tokens_but_keep_unknown_calls_reserved():
    config = FixedConfig(max_model_calls=3, total_generation_tokens=1000, generation_tokens=512)
    budget = RunBudget(config)
    first = budget.reserve_model("json", {}, 512)
    budget.record_model(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}, first
    )
    second = budget.reserve_model("json", {}, 512)
    budget.record_model({"usage": None}, second)
    assert budget.snapshot()["completion_tokens_reserved_or_used"] == 562
    third = budget.reserve_model("json", {}, 512)
    assert third == 438
    with pytest.raises(ModelCallFailed, match="model_call_budget"):
        budget.reserve_model("json", {}, 128)
    assert budget.snapshot()["provider_usage_totals"]["total_tokens"] is None
    assert not budget.snapshot()["usage_complete"]


def test_aggregate_prompt_limit_applies_even_if_each_prompt_fits():
    config = FixedConfig(max_model_calls=3, prompt_characters=5000, total_prompt_characters=5000)
    budget = RunBudget(config)
    reserved = budget.reserve_model("json", {"text": "x" * 3000}, 256)
    budget.record_model({"usage": {"completion_tokens": 10}}, reserved)
    with pytest.raises(ModelCallFailed, match="prompt_budget"):
        budget.reserve_model("json", {"text": "x" * 3000}, 256)
    assert budget.model_calls == 1


def test_wall_limit_is_checked_between_operations_without_claiming_inflight_cancellation():
    current = [0.0]
    budget = RunBudget(FixedConfig(wall_seconds=1), clock=lambda: current[0])
    current[0] = 2
    with pytest.raises(ModelCallFailed, match="wall_budget"):
        budget.check_time()
    assert budget.snapshot()["wall_limit_is_checked_between_operations"]


def test_provider_failure_does_not_fabricate_usage():
    budget = RunBudget(FixedConfig())
    budget.reserve_model("json", {}, 256)
    assert not budget.snapshot()["usage_complete"]
    assert budget.snapshot()["provider_usage_totals"]["total_tokens"] is None

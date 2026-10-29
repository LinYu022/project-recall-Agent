"""Contract, mutation and advisory-input checks, not evidence of learned general skill."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from investigation.agent_contracts import Decision
from investigation.contracts import InvestigationRequest
from memory.procedure_contracts import (
    Procedure,
    ValidateProcedure,
    compile_procedure,
    missing_preconditions,
)
from memory.procedure_engine import event_groups
from memory.procedure_runtime import augment_skill
from retrieval.contracts import SearchScope
from tests.test_single_agent import decision


def definition(**changes):
    return Procedure.model_validate(
        {
            "release_ids": ["a" * 64],
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }
        | changes
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"actions": ["exec_shell"]},
        {"allowed_tools": ["http_request"]},
        {"on_failure": "ignore_authorization"},
        {"parameters": ["api_key"]},
        {"expires_at": "2026-10-29T00:00:00"},
        {"arbitrary_python": "run()"},
    ],
)
def test_malicious_procedures_cannot_express_code_or_external_authority(changes):
    with pytest.raises(ValidationError):
        definition(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"actions": ["search", "expand"]},
        {"allowed_tools": ["lookup", "search"]},
        {"postconditions": []},
        {"required_fields": ["make", "model"]},
    ],
)
def test_incomplete_candidate_never_compiles_for_execution(changes):
    with pytest.raises(ValueError):
        compile_procedure(definition(**changes))


def test_deleting_a_precondition_would_match_unknown_year_but_validator_blocks_it():
    request = InvestigationRequest(
        task_id="SYNTHETIC", question="SYNTHETIC", retrieval_query="brake", intent="record_lookup"
    )
    scope = SearchScope(make="TOYOTA", model="PRIUS")
    safe = definition()
    unsafe = definition(required_fields=["make", "model"])
    assert "model_year" in missing_preconditions(safe, scope, request, "a" * 64)
    assert not missing_preconditions(unsafe, scope, request, "a" * 64)
    with pytest.raises(ValueError):
        compile_procedure(unsafe)


def test_known_related_campaigns_are_one_event_family():
    cards = [
        {
            "kind": "structured_lookup",
            "payload": {
                "rows": [
                    {"channel": "matched", "campaign_number": "17V331000"},
                    {"channel": "matched", "campaign_number": "17V652000"},
                ]
            },
        }
    ]
    assert len(event_groups(cards)) == 1


def test_application_action_has_no_arbitrary_parameters_and_default_off_is_exact():
    Decision.model_validate(decision("apply_skill"))
    with pytest.raises(ValidationError):
        Decision.model_validate(decision("apply_skill", query="outside task"))
    payload = {"question": "SYNTHETIC"}
    system, actual = augment_skill("old", payload)
    assert system == "old" and actual is payload


def test_transfer_batch_cannot_create_unbounded_research_allowance():
    with pytest.raises(ValidationError):
        ValidateProcedure(
            expected_revision=1,
            definition_sha256="a" * 64,
            target_task_ids=["T" + str(n) for n in range(5)],
            idempotency_key="limit",
        )

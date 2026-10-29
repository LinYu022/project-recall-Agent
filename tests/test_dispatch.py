"""Synthetic contracts and replay fingerprints, with no database or model requests."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from control.journal import canonical_input
from control.queue_contracts import Enqueue, Reconcile, add, fits, vector


@pytest.mark.parametrize("value", [True, -1, 0.1, "1"])
def test_resource_units_are_nonnegative_strict_integers(value):
    with pytest.raises(ValueError):
        vector(model_calls=value)


def test_shared_reservation_uses_used_plus_held_and_only_releases_unused_part():
    limits = vector(model_calls=2, tool_calls=3, completion_tokens=1000)
    held = vector(model_calls=1, completion_tokens=700)
    assert not fits(limits, vector(), held, vector(model_calls=1, completion_tokens=400))
    used = vector(model_calls=1, completion_tokens=150)
    assert fits(limits, used, vector(), vector(model_calls=1, completion_tokens=700))
    with pytest.raises(ValueError):
        add(held, limits, -1)


@pytest.mark.parametrize(
    "change",
    [
        {"max_attempts": 6},
        {"lease_seconds": 1},
        {"idempotency_key": "../../secret"},
        {"owner_id": "Other"},
        {"expected_version": True},
    ],
)
def test_dispatch_cannot_accept_owner_paths_or_unbounded_retries(change):
    with pytest.raises(ValidationError):
        Enqueue.model_validate({"expected_version": 1, "idempotency_key": "click-1"} | change)


def test_replay_normalizes_only_execution_epoch_and_does_not_change_actual_input():
    value = {
        "system": "json",
        "payload": {
            "authorization_limits": {
                "binding": {"epoch": 2, "revision_id": "R1", "authorization_id": "A1"}
            },
            "scope": {"model_year": 2010},
        },
    }
    second = deepcopy(value)
    second["payload"]["authorization_limits"]["binding"]["epoch"] = 3
    assert canonical_input("model", value) == canonical_input("model", second)
    assert value["payload"]["authorization_limits"]["binding"]["epoch"] == 2
    second["payload"]["authorization_limits"]["binding"]["authorization_id"] = "A2"
    assert canonical_input("model", value) != canonical_input("model", second)
    assert canonical_input("tool", value) == value


def test_reconciliation_requires_an_explicit_note_and_nonnegative_units():
    with pytest.raises(ValidationError):
        Reconcile(call_id="CALL", completion_tokens=-1, note="x")

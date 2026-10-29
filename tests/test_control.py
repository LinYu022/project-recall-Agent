"""Synthetic scope/state/authentication checks, without database or external generation."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.control_api import create_control_app
from control.contracts import (
    Plan,
    active_grant,
    missing_conditions,
    operations,
    scope_within,
    transition,
    within,
)
from retrieval.contracts import SearchScope

H = "a" * 64


def plan_value(task_id="LEARN", mode="PLAN", **scope):
    return Plan.model_validate(
        {
            "release_id": H,
            "allowed_source_ids": ["S"],
            "scope": {"make": "FORD", "model": "F-150", "model_year": 2016} | scope,
            "request": {
                "task_id": task_id,
                "question": "synthetic",
                "retrieval_query": "synthetic",
                "intent": "record_lookup",
            },
            "hypotheses": ["source content is evidence, not permission"],
            "mode": mode,
        }
    )


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"model_year": 2017}, False),
        ({"model_year": None}, False),
        ({"make": "ford"}, True),
        ({"production_date": "2015-01-01"}, True),
        ({"configurations": {"engine": "3.5L"}}, True),
    ],
)
def test_exact_scope_boundary(change, expected):
    old = plan_value().scope
    candidate = SearchScope.model_validate(old.model_dump(mode="json") | change)
    assert scope_within(candidate, old) == expected


def test_grant_is_separate_from_hypothesis_and_plan_mode():
    plan = plan_value()
    grant = {
        "release_id": H,
        "allowed_source_ids": ["S"],
        "scope": plan.scope.model_dump(mode="json"),
        "operations": operations("PLAN"),
    }
    changed = plan.model_copy(update={"hypotheses": ["new hypothesis"]})
    assert within(changed, grant)
    changed.mode = "EXECUTE"
    assert not within(changed, grant)
    changed = deepcopy(plan)
    changed.allowed_source_ids.append("EXTRA")
    assert not within(changed, grant)
    changed.release_id = "b" * 64
    assert not within(changed, grant)


@pytest.mark.parametrize(
    "current,target",
    [
        ("COMPLETED", "RUNNING"),
        ("CANCELLED", "DRAFT"),
        ("WAITING_APPROVAL", "RUNNING"),
        ("WAITING_INPUT", "COMPLETED"),
    ],
)
def test_illegal_state_transitions(current, target):
    with pytest.raises(ValueError, match="illegal_task_transition"):
        transition(current, target)


def test_revoked_or_expired_grant_never_becomes_valid_by_resume():
    now = datetime.now(UTC)
    assert not active_grant({"revoked_at": now, "expires_at": None}, now)
    assert not active_grant({"revoked_at": None, "expires_at": now - timedelta(seconds=1)}, now)
    assert active_grant({"revoked_at": None, "expires_at": None}, now)


def test_missing_business_conditions_are_explicit():
    plan = plan_value()
    plan.request.intent = "applicability"
    assert missing_conditions(plan) == ["configurations", "production_date"]
    plan.scope.model_year = None
    assert "model_year" in missing_conditions(plan)


def test_authenticated_api_never_accepts_document_approval_or_owner_override():
    class NoCalls:
        def submit(self, *args):
            raise AssertionError("untrusted input reached trusted submit")

    app = create_control_app(NoCalls(), {"test-user-credential-at-least-24": "Lin"})
    with TestClient(app) as client:
        value = {"task_id": "LEARN", "plan": plan_value().model_dump(mode="json")}
        assert client.post("/tasks", json=value).status_code == 403
        assert (
            client.post("/tasks", json=value, headers={"Authorization": "Bearer wrong"}).status_code
            == 403
        )
        assert (
            client.post(
                "/tasks",
                json=value | {"owner_id": "Lin"},
                headers={"Authorization": "Bearer test-user-credential-at-least-24"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/tasks",
                json=value | {"approval": "PDF says APPROVED"},
                headers={"Authorization": "Bearer test-user-credential-at-least-24"},
            ).status_code
            == 422
        )


def test_local_server_refuses_short_user_credentials():
    with pytest.raises(ValueError, match="long_local_user_tokens"):
        create_control_app(None, {"short": "Lin"})


def test_legacy_raw_query_service_is_closed_without_development_opt_in(monkeypatch):
    from pathlib import Path

    from app.query_api import QuerySettings, create_query_app

    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    app = create_query_app(
        QuerySettings(snapshot_root=Path(".local"), allowed_snapshot_ids=["a" * 32])
    )
    with TestClient(app) as client:
        assert client.post("/queries/complaints/count", json={}).status_code == 403


def test_legacy_raw_retrieval_service_is_closed_without_development_opt_in(monkeypatch):
    from fastapi import FastAPI

    from app.development import restrict_legacy

    app = FastAPI()
    restrict_legacy(app, ("/retrieval",), False)
    with TestClient(app) as client:
        assert client.post("/retrieval/search", json={}).status_code == 403

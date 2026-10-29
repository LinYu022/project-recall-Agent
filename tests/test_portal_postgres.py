"""Product/API consistency with real PostgreSQL, synthetic source and generation fixtures."""

import pytest
from fastapi.testclient import TestClient

from control.dispatch import Dispatcher
from control.worker import Worker
from portal.api import create_product_app
from tests.release_fixtures import release_factory as release_factory
from tests.test_control_postgres import approval_for
from tests.test_control_postgres import controlled as controlled

pytestmark = pytest.mark.postgres
TOKEN = "synthetic-product-user-credential-24-plus"
OTHER = "synthetic-second-user-credential-24-plus"


@pytest.fixture
def product(controlled):
    control, submit, bundle = controlled
    dispatcher = Dispatcher(control)
    app = create_product_app(control, {TOKEN: "Lin", OTHER: "other"}, profile="synthetic-demo")
    client = TestClient(app, headers={"Authorization": "Bearer " + TOKEN})
    return client, control, submit, bundle, dispatcher


def test_history_list_owner_isolation_and_same_version_rules(product):
    client, control, submit, bundle, _ = product
    first = submit()
    assert client.get("/product/tasks").json()["tasks"][0]["task_id"] == "LEARN"
    assert (
        client.get("/product/catalog").json()["releases"][0]["release_id"]
        == bundle["manifest"]["release_id"]
    )
    assert client.get("/product/tasks?limit=101").status_code == 422
    assert (
        client.get("/product/tasks", headers={"Authorization": "Bearer " + OTHER}).json()["tasks"]
        == []
    )
    assert (
        client.get("/tasks/LEARN", headers={"Authorization": "Bearer " + OTHER}).status_code == 403
    )
    assert (
        client.post("/tasks/LEARN/pause", json={"expected_version": first["version"]}).status_code
        == 200
    )
    assert (
        client.post("/tasks/LEARN/resume", json={"expected_version": first["version"]}).status_code
        == 409
    )
    assert control.view("Lin", "LEARN")["state"] == "PAUSED"


def test_report_and_original_share_revocation_and_source_version_gate(product):
    client, control, submit, _, dispatcher = product
    first = submit(mode="EXECUTE")
    enqueue = client.post("/tasks/LEARN/execute", json={"expected_version": first["version"]})
    assert enqueue.status_code == 200
    Worker(dispatcher, "product-test").run_once(job_id=enqueue.json()["job_id"])
    report = client.get("/tasks/LEARN/report").json()
    assert report["generation_mode"] == "simulated_model"
    cards = client.get("/product/tasks/LEARN/evidence").json()
    alias = next(
        a
        for a, key in cards["aliases"].items()
        if next(c for c in cards["cards"] if c["evidence_id"] == key)["kind"] == "text"
    )
    original = client.get(f"/product/tasks/LEARN/original/{alias}").json()
    assert original["locations"][0]["status"] == "verified"
    assert "SYNTHETIC" in original["locations"][0]["quote"]
    assert client.get("/product/tasks/LEARN/original/E999").status_code == 404
    assert (
        client.get(
            f"/product/tasks/LEARN/original/{alias}", headers={"Authorization": "Bearer " + OTHER}
        ).status_code
        == 403
    )
    done = control.view("Lin", "LEARN")
    assert (
        client.post("/tasks/LEARN/revoke", json={"expected_version": done["version"]}).status_code
        == 200
    )
    for path in [
        "/tasks/LEARN/report",
        "/product/tasks/LEARN/evidence",
        f"/product/tasks/LEARN/original/{alias}",
    ]:
        assert client.get(path).status_code == 403


def test_ui_approval_must_bind_exact_revision(product):
    client, control, submit, _, _ = product
    first = submit()
    plan = first["plan"] | {"mode": "EXECUTE"}
    waiting = client.post(
        "/tasks/LEARN/revisions", json={"expected_version": first["version"], "plan": plan}
    ).json()
    assert waiting["state"] == "WAITING_APPROVAL"
    stale = approval_for(waiting).model_dump(mode="json")
    changed = client.post(
        "/tasks/LEARN/revisions",
        json={"expected_version": waiting["version"], "plan": plan | {"steps": ["changed"]}},
    ).json()
    assert client.post("/tasks/LEARN/approvals", json=stale).status_code == 409
    assert (
        client.post(
            "/tasks/LEARN/approvals", json=approval_for(changed).model_dump(mode="json")
        ).status_code
        == 200
    )
    assert control.view("Lin", "LEARN")["authorization"]["active"]


def test_memory_provenance_owner_and_stale_revoke(product):
    client, _, _, _, _ = product
    source = client.post(
        "/memory/sources", json={"origin": "user_statement", "text": "SYNTHETIC preference"}
    ).json()
    candidate = client.post(
        "/memory/candidates",
        json={
            "source_id": source["source_id"],
            "proposal": {
                "kind": "preference",
                "scope": "user",
                "semantic_key": "language",
                "value": "SYNTHETIC English",
            },
        },
    ).json()
    reviewed = client.post(
        f"/memory/candidates/{candidate['candidate_id']}/review",
        json={
            "expected_revision": candidate["revision"],
            "proposal_sha256": candidate["proposal_sha256"],
            "decision": "approve",
            "note": "SYNTHETIC user review",
        },
    )
    assert reviewed.status_code == 200
    item = client.get("/product/memories").json()["items"][0]
    key = item["memory_id"]
    assert (
        client.get(f"/product/memories/{key}/source").json()["source"]["text"]
        == "SYNTHETIC preference"
    )
    assert (
        client.get(
            f"/product/memories/{key}/source", headers={"Authorization": "Bearer " + OTHER}
        ).status_code
        == 403
    )
    assert (
        client.get("/product/memories", headers={"Authorization": "Bearer " + OTHER}).json()[
            "items"
        ]
        == []
    )
    payload = {
        "expected_revision": item["revision"],
        "action": "disable",
        "note": "SYNTHETIC revoke",
    }
    assert client.post(f"/memory/items/{key}/change", json=payload).status_code == 200
    assert client.post(f"/memory/items/{key}/change", json=payload).status_code == 409
    assert client.get("/product/memories").json()["items"][0]["state"] == "disabled"

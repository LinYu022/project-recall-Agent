"""Synthetic snapshot API checks. Database/count success is tested against PostgreSQL separately."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.query_api import QuerySettings, create_query_app
from ingestion.snapshots import SnapshotStore
from tests.source_fixtures import capture_rows, complaint
from tests.test_entity_links import rules, source


def temporal(ids, *, cutoff=None, interval=None):
    return {
        "snapshot_ids": ids,
        "time": {
            "mode": "historical_as_of" if cutoff else "snapshot_analysis",
            "knowledge_cutoff": cutoff,
            "event_interval": interval,
            "date_filter_basis": "SYNTHETIC API boundary check",
        },
    }


def count_body(identifier, **updates):
    return {
        "temporal": temporal([identifier]),
        "make": "FORD",
        "model": "F-150",
        "model_year": 2016,
        "components": ["SERVICE BRAKES"],
        **updates,
    }


@pytest.fixture
def query_api(tmp_path, monkeypatch):
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    left, _ = source(tmp_path, "17V331000")
    right, _ = source(tmp_path, "17V652000")
    complaint_snapshot = capture_rows(tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])
    ids = [left.snapshot_id, right.snapshot_id, complaint_snapshot.snapshot_id]
    settings = QuerySettings(snapshot_root=tmp_path, allowed_snapshot_ids=ids)
    app = create_query_app(settings, scope_rules=rules(), development_mode=True)
    body = {"temporal": temporal(ids[:2]), "left": left.model_dump(), "right": right.model_dump()}
    with TestClient(app) as client:
        yield client, app, body, complaint_snapshot


def test_link_query_is_read_only_and_returns_bound_evidence(query_api, tmp_path):
    client, _, body, _ = query_api
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    response = client.post("/queries/entity-links", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "assessed" and result["temporal_status"] == "selected"
    assert result["time"]["mode"] == "snapshot_analysis" and result["dataset_release"] is None
    assert result["assessment"]["links"][0]["evidence"]
    assert all(link["causation"] == "not_established" for link in result["assessment"]["links"])
    assert client.post("/queries/entity-links", json=body).json() == result
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == before


def test_link_historical_cutoff_cannot_use_future_versions(query_api):
    client, _, body, _ = query_api
    body["temporal"] = temporal(body["temporal"]["snapshot_ids"], cutoff="2000-01-01T00:00:00Z")
    response = client.post("/queries/entity-links", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "unsupported" and result["assessment"] is None
    assert result["reason"] == "temporal_selection_incomplete"


def test_bounded_observed_history_is_explicitly_labeled(query_api):
    client, _, body, _ = query_api
    body["temporal"] = temporal(body["temporal"]["snapshot_ids"], cutoff="2100-01-01T00:00:00Z")
    result = client.post("/queries/entity-links", json=body).json()
    assert result["status"] == "assessed"
    assert result["analysis_label"] == "bounded_observed_versions_as_of"
    assert result["coverage"] == "selected_local_receipts_only"


def test_event_dates_unavailable_does_not_return_positive_recall_links(query_api):
    client, _, body, _ = query_api
    body["temporal"]["time"]["event_interval"] = {"start": "2016-01-01", "end": "2016-12-31"}
    result = client.post("/queries/entity-links", json=body).json()
    assert result["status"] == "unsupported" and result["assessment"] is None
    assert result["exclusion_reasons"]["event_date_unavailable"] == 2


def test_empty_event_selection_does_not_assess_unselected_endpoints(tmp_path, monkeypatch):
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    a = capture_rows(
        tmp_path, [complaint(12345678, event="01/02/2020"), complaint(12345679, event="01/02/2020")]
    )
    t = temporal([a.snapshot_id], interval={"start": "2000-01-01", "end": "2001-01-01"})
    app = create_query_app(
        QuerySettings(snapshot_root=tmp_path, allowed_snapshot_ids=[a.snapshot_id]),
        scope_rules=rules(),
        development_mode=True,
    )
    body = {
        "temporal": t,
        "left": {"snapshot_id": a.snapshot_id, "ordinal": 0},
        "right": {"snapshot_id": a.snapshot_id, "ordinal": 1},
    }
    with TestClient(app) as client:
        result = client.post("/queries/entity-links", json=body).json()
    assert result["status"] == "unsupported" and result["reason"] == "endpoint_not_selected"


@pytest.mark.parametrize(
    "field,value",
    [("root", "PRIVATE_PATH"), ("sql", "DROP TABLE x"), ("rules", {"confirmed": True})],
)
def test_caller_cannot_set_paths_sql_or_rules(query_api, field, value):
    client, _, body, _ = query_api
    response = client.post("/queries/entity-links", json={**body, field: value})
    assert response.status_code == 422 and "PRIVATE_PATH" not in response.text


def test_endpoints_must_belong_to_pinned_input(query_api):
    client, _, body, _ = query_api
    body["temporal"]["snapshot_ids"] = [body["left"]["snapshot_id"]]
    assert client.post("/queries/entity-links", json=body).status_code == 422


def test_unknown_and_unregistered_snapshots_have_different_errors(query_api):
    client, app, _, _ = query_api
    request = count_body("0" * 32)
    assert client.post("/queries/complaints/count", json=request).status_code == 403
    app.state.query_services.settings.allowed_snapshot_ids.append("0" * 32)
    response = client.post("/queries/complaints/count", json=request)
    assert response.status_code == 404 and response.json()["detail"] == "snapshot_not_found"


def test_missing_body_and_tampered_content_are_integrity_failures(query_api):
    client, app, _, snapshot = query_api
    path = app.state.query_services.settings.snapshot_root / "objects" / snapshot.raw.sha256
    body = count_body(snapshot.snapshot_id)
    path.write_bytes(b"tampered")
    assert client.post("/queries/complaints/count", json=body).status_code == 409
    path.unlink()
    assert client.post("/queries/complaints/count", json=body).status_code == 409


def test_invalid_query_does_not_echo_sql_like_input(query_api):
    client, _, _, snapshot = query_api
    response = client.post(
        "/queries/complaints/count",
        json=count_body(
            snapshot.snapshot_id, model="PRIVATE_INPUT'; DROP TABLE record_versions; --"
        ),
    )
    assert response.status_code == 422 and "PRIVATE_INPUT" not in response.text


def test_count_without_database_is_service_failure_not_zero(query_api):
    client, _, _, snapshot = query_api
    response = client.post("/queries/complaints/count", json=count_body(snapshot.snapshot_id))
    assert response.status_code == 503
    assert response.json() == {"status": "query_database_unavailable"}


def test_historically_unsupported_count_does_not_need_database(query_api):
    client, _, _, snapshot = query_api
    body = count_body(
        snapshot.snapshot_id,
        temporal=temporal([snapshot.snapshot_id], cutoff="2000-01-01T00:00:00Z"),
    )
    response = client.post("/queries/complaints/count", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"
    assert response.json()["distinct_complaints"] is None


def test_health_is_configuration_status_and_openapi_has_only_queries(query_api):
    client, _, _, _ = query_api
    health = client.get("/health").json()
    assert health["data_mode"] == "saved_source_snapshots"
    assert health["database_configured"] is False and health["database_connection_checked"] is False
    assert health["model_provider"] is None
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {"/health", "/queries/complaints/count", "/queries/entity-links"}
    assert client.post("/demo/runs", json={}).status_code == 404


def test_config_is_server_side_and_has_unique_snapshot_selection(query_api, tmp_path, monkeypatch):
    _, app, _, _ = query_api
    settings = app.state.query_services.settings
    path = tmp_path / "config.json"
    path.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("RECALL_QUERY_CONFIG", str(path))
    with TestClient(create_query_app(scope_rules=rules(), development_mode=True)) as client:
        assert client.get("/health").json()["allowed_snapshots"] == 3
    with pytest.raises(ValidationError):
        QuerySettings(snapshot_root=tmp_path, allowed_snapshot_ids=["0" * 32, "0" * 32])


def test_repeated_endpoint_is_invalid(query_api):
    client, _, body, _ = query_api
    body["right"] = body["left"]
    assert client.post("/queries/entity-links", json=body).status_code == 422


def test_invalid_ordinal_is_not_an_empty_link_result(query_api):
    client, _, body, _ = query_api
    body["right"]["ordinal"] = 100
    response = client.post("/queries/entity-links", json=body)
    assert response.status_code == 422 and response.json()["status"] == "invalid_query"


def test_later_receipt_cannot_supply_citation_for_earlier_cutoff(tmp_path, monkeypatch):
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    left, _ = source(tmp_path, "17V331000")
    right, _ = source(tmp_path, "17V652000")
    repeated, _ = source(tmp_path, "17V331000")
    cutoff = SnapshotStore(tmp_path).verify(right.snapshot_id).fetched_at.isoformat()
    ids = [left.snapshot_id, right.snapshot_id, repeated.snapshot_id]
    app = create_query_app(
        QuerySettings(snapshot_root=tmp_path, allowed_snapshot_ids=ids),
        scope_rules=rules(),
        development_mode=True,
    )
    body = {
        "temporal": temporal(ids, cutoff=cutoff),
        "left": repeated.model_dump(),
        "right": right.model_dump(),
    }
    with TestClient(app) as client:
        result = client.post("/queries/entity-links", json=body).json()
    assert result["status"] == "unsupported"
    assert result["reason"] == "endpoint_receipt_outside_time_scope"


def test_same_record_key_with_conflicting_contents_cannot_yield_positive_links(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    row = {
        "NHTSACampaignNumber": "10V039000",
        "Make": "FORD",
        "Model": "F-150",
        "ModelYear": "2016",
        "Component": "SERVICE BRAKES",
        "Summary": "SYNTHETIC old",
    }
    snapshot = capture_rows(
        tmp_path, [row, row | {"Summary": "SYNTHETIC changed"}], kind="recall_api"
    )
    app = create_query_app(
        QuerySettings(snapshot_root=tmp_path, allowed_snapshot_ids=[snapshot.snapshot_id]),
        scope_rules=rules(),
        development_mode=True,
    )
    body = {
        "temporal": temporal([snapshot.snapshot_id]),
        "left": {"snapshot_id": snapshot.snapshot_id, "ordinal": 0},
        "right": {"snapshot_id": snapshot.snapshot_id, "ordinal": 1},
    }
    with TestClient(app) as client:
        result = client.post("/queries/entity-links", json=body).json()
    assert result["status"] == "unsupported" and result["assessment"] is None
    assert result["temporal_status"] == "partial"

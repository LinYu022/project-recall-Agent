"""Offline mapping checks use synthetic rows, not live source responses."""

import json

import pytest

from ingestion.__main__ import main
from ingestion.records import content_hash, date_field, prepare_import
from ingestion.snapshots import FetchResult, SnapshotStore
from ingestion.sources import load_sources
from tests.source_fixtures import capture_rows, complaint, recall


@pytest.mark.parametrize(
    "raw,value,status",
    [
        ("01/02/2020", "2020-01-02", "parsed"),
        ("02/29/2020", "2020-02-29", "parsed"),
        ("02/29/2021", None, "invalid"),
        ("2020-01-02", None, "invalid"),
        (None, None, "missing"),
        ("", None, "missing"),
    ],
)
def test_date_mapping_preserves_original_and_distinguishes_unknown(raw, value, status):
    mapped = date_field(raw, format_verified=True)
    assert (mapped["raw"], mapped["value"], mapped["status"]) == (raw, value, status)


def test_recall_date_is_not_guessed_and_row_key_is_vehicle_component_specific(tmp_path):
    rows = [recall(), recall() | {"Model": "OTHER"}]
    snapshot = capture_rows(tmp_path, rows, "recall_api")
    batch = prepare_import(tmp_path, snapshot.snapshot_id)
    assert batch.report["accepted"] == 2
    assert batch.report["distinct_record_keys"] == 2
    assert batch.report["date_issues"] == {"format_unverified": 2}
    field = batch.rows[0].version["normalized"]["dates"]["report_received_date"]
    assert field["raw"] == "09/02/2010" and field["value"] is None


@pytest.mark.parametrize(
    "change,code",
    [
        ({"odiNumber": True}, "odi_number_invalid"),
        ({"summary": None}, "required_text_missing"),
        ({"products": []}, "products_invalid"),
        ({"products": [{"productMake": "OTHER"}]}, "row_outside_selection"),
    ],
)
def test_invalid_rows_are_isolated_and_counts_reconcile(tmp_path, change, code):
    snapshot = capture_rows(tmp_path, [complaint(), complaint() | change])
    batch = prepare_import(tmp_path, snapshot.snapshot_id)
    assert batch.report["input_units"] == batch.report["accepted"] + batch.report["rejected"] == 2
    assert batch.report["accepted"] == batch.report["rejected"] == 1
    assert batch.rows[1].error_code == code
    assert batch.rows[1].pointer == "/results/1"
    assert "SYNTHETIC COMPLAINT" not in json.dumps(batch.report)


def test_invalid_optional_date_keeps_record_but_marks_quality_issue(tmp_path):
    snapshot = capture_rows(tmp_path, [complaint(event="impossible")])
    batch = prepare_import(tmp_path, snapshot.snapshot_id)
    assert batch.report["accepted"] == 1 and batch.report["date_issues"] == {"invalid": 1}
    assert batch.rows[0].version["normalized"]["version_public_at"] is None


def test_key_order_does_not_change_semantic_hash_but_unknown_fields_do(tmp_path):
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    first = capture_rows(tmp_path, [complaint()])
    second = capture_rows(tmp_path, [complaint() | {"new_unknown_field": True}])
    a = prepare_import(tmp_path, first.snapshot_id).rows[0].version
    b = prepare_import(tmp_path, second.snapshot_id).rows[0].version
    assert a["record_key"] == b["record_key"] and a["version_id"] != b["version_id"]
    assert b["payload"]["new_unknown_field"] is True


def test_document_import_only_registers_file_version(tmp_path):
    source = load_sources()["NHTSA-INV-PE10006-CLOSE"]
    snapshot = SnapshotStore(tmp_path).capture(
        source, lambda _: FetchResult(200, {}, b"%PDF-SYNTHETIC")
    )
    batch = prepare_import(tmp_path, snapshot.snapshot_id)
    assert batch.report["input_units"] == batch.report["accepted"] == 1
    assert batch.report["container_record_count"] is None
    assert batch.rows[0].pointer == "$document"
    assert batch.rows[0].version["normalized"]["text_parse_status"] == "not_started"


def test_failed_capture_cannot_become_an_empty_successful_import(tmp_path):
    source = load_sources()["NHTSA-COMPLAINTS-F150-2016"]
    snapshot = SnapshotStore(tmp_path).capture(
        source, lambda _: FetchResult(503, {}, b"unavailable")
    )
    with pytest.raises(ValueError, match="captured"):
        prepare_import(tmp_path, snapshot.snapshot_id)


def test_inspect_cli_reports_rejections_without_opening_a_database(tmp_path, monkeypatch, capsys):
    snapshot = capture_rows(tmp_path, [complaint() | {"odiNumber": None}])
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["ingestion", "inspect", snapshot.snapshot_id, "--root", str(tmp_path)]
    )
    assert main() == 4
    assert json.loads(capsys.readouterr().out)["rejected"] == 1

"""Synthetic acquisition checks; no requests to public sites in CI."""

import json
from http.client import IncompleteRead
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from ingestion.__main__ import main
from ingestion.snapshots import FetchResult, NoRedirects, SnapshotStore, fetch_http
from ingestion.sources import load_sources
from runtime.artifacts import ArtifactIntegrityError


@pytest.fixture
def source():
    return load_sources()["NHTSA-API-CAMPAIGN-10V039000"]


def json_body(count=1, summary="SYNTHETIC TEST RECORD"):
    return json.dumps({"Count": count, "results": [{"Summary": summary}] if count else []}).encode()


def response(body, status=200):
    return FetchResult(status, {"content-type": "application/json"}, body)


def test_same_bytes_reuse_object_but_preserve_each_fetch_receipt(tmp_path, source):
    store = SnapshotStore(tmp_path)
    body = json_body()
    first = store.capture(source, lambda _: response(body))
    receipt = (tmp_path / "snapshots" / f"{first.snapshot_id}.json").read_bytes()
    second = store.capture(source, lambda _: response(body))
    assert first.snapshot_id != second.snapshot_id
    assert first.raw == second.raw
    assert (tmp_path / "snapshots" / f"{first.snapshot_id}.json").read_bytes() == receipt
    assert len(list((tmp_path / "objects").iterdir())) == 1
    assert len(list((tmp_path / "snapshots").iterdir())) == 2
    assert store.raw_bytes(first.raw) == body
    assert store.verify(first.snapshot_id) == first
    assert store.verify(second.snapshot_id) == second


def test_changed_content_never_overwrites_original(tmp_path, source):
    store = SnapshotStore(tmp_path)
    before = json_body(summary="SYNTHETIC ORIGINAL")
    after = json_body(summary="SYNTHETIC REVISION")
    first = store.capture(source, lambda _: response(before))
    second = store.capture(source, lambda _: response(after))
    assert first.source.source_id == second.source.source_id
    assert first.raw.sha256 != second.raw.sha256
    assert store.raw_bytes(first.raw) == before
    assert store.raw_bytes(second.raw) == after


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"Count":1,"results":[]}',
        b'{"Count":true,"results":[]}',
        b'{"Count":1,"results":[null]}',
    ],
)
def test_bad_payload_is_saved_but_not_counted_as_empty(tmp_path, source, body):
    store = SnapshotStore(tmp_path)
    snapshot = store.capture(source, lambda _: response(body))
    assert snapshot.status == "invalid_payload"
    assert snapshot.record_count is None
    assert snapshot.raw is not None
    assert store.raw_bytes(snapshot.raw) == body
    assert store.verify(snapshot.snapshot_id) == snapshot


def test_http_error_preserves_body_and_explicit_failure(tmp_path, source):
    store = SnapshotStore(tmp_path)
    snapshot = store.capture(source, lambda _: response(b"SERVICE UNAVAILABLE", 503))
    assert snapshot.status == "http_error"
    assert snapshot.http_status == 503
    assert snapshot.record_count is None
    assert store.raw_bytes(snapshot.raw) == b"SERVICE UNAVAILABLE"


@pytest.mark.parametrize(
    "error,status",
    [
        (TimeoutError("PRIVATE UPSTREAM MESSAGE"), "transport_error"),
        (URLError("network failed"), "transport_error"),
        (IncompleteRead(b"partial"), "transport_error"),
    ],
)
def test_transport_failure_has_no_complete_raw_object(tmp_path, source, error, status):
    def fetch(_):
        raise error

    store = SnapshotStore(tmp_path)
    snapshot = store.capture(source, fetch)
    assert snapshot.status == status
    assert snapshot.raw is None and snapshot.fetched_at is None
    assert snapshot.record_count is None
    assert "PRIVATE UPSTREAM MESSAGE" not in snapshot.model_dump_json()


def test_byte_limit_does_not_publish_truncated_content(tmp_path, source):
    store = SnapshotStore(tmp_path)
    source = source.model_copy(update={"max_bytes": 4})
    snapshot = store.capture(source, lambda _: response(b"TOO MANY BYTES"))
    assert snapshot.status == "too_large"
    assert snapshot.raw is None and snapshot.record_count is None
    assert not (tmp_path / "objects").exists()


def test_successful_zero_results_is_distinct_from_failure(tmp_path, source):
    snapshot = SnapshotStore(tmp_path).capture(source, lambda _: response(json_body(0)))
    assert snapshot.status == "captured" and snapshot.record_count == 0
    assert snapshot.error_code is None


def test_cache_headers_and_old_record_dates_are_not_publication_proof(tmp_path, source):
    body = b'{"Count":1,"results":[{"ReportReceivedDate":"09/02/2010"}]}'
    result = FetchResult(200, {"last-modified": "Tue, 09 Feb 2010 12:00:00 GMT"}, body)
    snapshot = SnapshotStore(tmp_path).capture(source, lambda _: result)
    assert snapshot.version_public_at is None
    assert snapshot.historical_evidence == "local_fetch_only"
    assert snapshot.fetched_at == snapshot.completed_at


def test_complaints_use_lowercase_count_and_pdf_check_is_only_signature(tmp_path):
    sources = load_sources()
    complaints = sources["NHTSA-COMPLAINTS-F150-2016"]
    pdf = sources["NHTSA-INV-PE10006-CLOSE"]
    store = SnapshotStore(tmp_path)
    assert (
        store.capture(complaints, lambda _: response(b'{"count":0,"results":[]}')).record_count == 0
    )
    assert store.capture(pdf, lambda _: response(b"<html>error</html>")).status == "invalid_payload"
    snapshot = store.capture(pdf, lambda _: response(b"%PDF-SYNTHETIC SIGNATURE ONLY"))
    assert snapshot.status == "captured" and snapshot.record_count is None
    assert snapshot.inspector_version == "raw-container/0.1"


def test_tampered_object_is_detected_and_never_silently_replaced(tmp_path, source):
    store = SnapshotStore(tmp_path)
    body = json_body()
    snapshot = store.capture(source, lambda _: response(body))
    (tmp_path / "objects" / snapshot.raw.sha256).write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError):
        store.verify(snapshot.snapshot_id)
    with pytest.raises(ArtifactIntegrityError):
        store.capture(source, lambda _: response(body))


def test_verify_cli_uses_local_bytes_only_and_rejects_path_traversal(
    tmp_path, source, monkeypatch, capsys
):
    store = SnapshotStore(tmp_path)
    snapshot = store.capture(source, lambda _: response(json_body()))
    monkeypatch.setattr(
        "sys.argv", ["ingestion", "verify", snapshot.snapshot_id, "--root", str(tmp_path)]
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["record_count"] == 1
    monkeypatch.setattr("sys.argv", ["ingestion", "verify", "../outside", "--root", str(tmp_path)])
    assert main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "snapshot_operation_failed"


def test_transport_bounds_response_and_preserves_http_errors(source, monkeypatch):
    class FakeResponse(BytesIO):
        status = 200
        headers = {"Content-Type": "application/json", "Content-Length": "9999"}

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Accept-encoding") == "identity"
            assert timeout == 30
            return FakeResponse(b"short")

    monkeypatch.setattr("ingestion.snapshots.build_opener", lambda _: Opener())
    with pytest.raises(OSError, match="Content-Length"):
        fetch_http(source)

    class ErrorOpener:
        def open(self, request, timeout):
            raise HTTPError(source.url, 503, "Unavailable", {}, BytesIO(b"error"))

    monkeypatch.setattr("ingestion.snapshots.build_opener", lambda _: ErrorOpener())
    result = fetch_http(source)
    assert result.status == 503 and result.body == b"error"
    request = Request(source.url)
    assert (
        NoRedirects().redirect_request(request, None, 302, "Redirect", {}, "https://example.com")
        is None
    )

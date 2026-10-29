"""Preserve original bytes and append immutable fetch receipts."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from ingestion.contracts import RawObject, Snapshot, SnapshotStatus, SourceSpec
from runtime.artifacts import ArtifactIntegrityError


class PayloadTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class FetchResult:
    status: int
    headers: dict[str, str]
    body: bytes


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Changed URLs need an explicit registry update, with a new receipt.
        return None


def fetch_http(source: SourceSpec) -> FetchResult:
    request = Request(
        source.url,
        headers={
            "User-Agent": "recall-evidence-agent/0.1 local-research",
            "Accept": "application/pdf"
            if source.kind == "investigation_pdf"
            else "application/json",
            "Accept-Encoding": "identity",
        },
    )
    try:
        response = build_opener(NoRedirects()).open(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(source.max_bytes + 1)
        if len(body) > source.max_bytes:
            raise PayloadTooLarge("response exceeded source byte limit")
        headers = {
            name.lower(): value
            for name, value in response.headers.items()
            if name.lower()
            in {
                "content-type",
                "content-length",
                "content-encoding",
                "date",
                "etag",
                "last-modified",
                "location",
            }
        }
        expected = headers.get("content-length")
        if expected is not None and int(expected) != len(body):
            raise OSError("response body length differs from Content-Length")
        return FetchResult(response.status, headers, body)


def inspect_payload(source: SourceSpec, body: bytes) -> int | None:
    """Check the raw container only; PDF text and record fields are later steps."""
    if source.kind == "investigation_pdf":
        if not body.startswith(b"%PDF-"):
            raise ValueError("pdf_signature_invalid")
        return None
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("json_envelope_invalid")
    count = value.get("Count" if source.kind == "recall_api" else "count")
    results = value.get("results")
    if type(count) is not int or count < 0 or not isinstance(results, list):
        raise ValueError("json_envelope_invalid")
    if count != len(results) or any(not isinstance(row, dict) for row in results):
        raise ValueError("json_count_or_rows_invalid")
    return count


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root

    def raw_bytes(self, ref: RawObject) -> bytes:
        body = (self.root / "objects" / ref.sha256).read_bytes()
        if len(body) != ref.byte_size or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("raw snapshot object hash or byte size differs")
        return body

    def _put_body(self, body: bytes) -> RawObject:
        ref = RawObject(sha256=hashlib.sha256(body).hexdigest(), byte_size=len(body))
        directory = self.root / "objects"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            with (directory / ref.sha256).open("xb") as stream:
                stream.write(body)
        except FileExistsError:
            if self.raw_bytes(ref) != body:
                raise ArtifactIntegrityError("existing raw object differs")
        return ref

    def capture(
        self, source: SourceSpec, fetch: Callable[[SourceSpec], FetchResult] = fetch_http
    ) -> Snapshot:
        started = datetime.now(UTC)
        result, ref, count, fetched_at = None, None, None, None
        error_code = None
        status: SnapshotStatus
        try:
            result = fetch(source)
            if len(result.body) > source.max_bytes:
                raise PayloadTooLarge("response exceeded source byte limit")
        except PayloadTooLarge:
            status, error_code = "too_large", "response_byte_limit"
        except (URLError, OSError, HTTPException, ValueError):
            status, error_code = "transport_error", "http_transport_error"
        else:
            # Storage failures propagate, rather than being mislabeled as network failures.
            ref = self._put_body(result.body)
            if result.status != 200:
                status, error_code = "http_error", "http_status_not_200"
            else:
                try:
                    count = inspect_payload(source, result.body)
                except (ValueError, UnicodeError):
                    status, error_code = "invalid_payload", "raw_container_invalid"
                else:
                    status = "captured"
        completed = datetime.now(UTC)
        if ref is not None:
            fetched_at = completed
        snapshot = Snapshot(
            snapshot_id=uuid4().hex,
            source=source,
            request_started_at=started,
            completed_at=completed,
            fetched_at=fetched_at,
            status=status,
            http_status=result.status if result else None,
            http_headers=result.headers if result else {},
            raw=ref,
            record_count=count,
            error_code=error_code,
        )
        directory = self.root / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{snapshot.snapshot_id}.json").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(snapshot.model_dump_json(indent=2) + "\n")
        return snapshot

    def verify(self, snapshot_id: str) -> Snapshot:
        # Validate before using the ID as a local path component.
        if len(snapshot_id) != 32 or any(c not in "0123456789abcdef" for c in snapshot_id):
            raise ValueError("invalid snapshot_id")
        snapshot = Snapshot.model_validate_json(
            (self.root / "snapshots" / f"{snapshot_id}.json").read_bytes()
        )
        if snapshot.snapshot_id != snapshot_id:
            raise ArtifactIntegrityError("snapshot identity differs from filename")
        if snapshot.raw is not None:
            body = self.raw_bytes(snapshot.raw)
            if (
                snapshot.status == "captured"
                and inspect_payload(snapshot.source, body) != snapshot.record_count
            ):
                raise ArtifactIntegrityError("replayed raw count differs from manifest")
        return snapshot

"""Durable pre-call reservations for a bounded, explicitly approved local experiment."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from models.chat_api import ModelCallFailed


class RequestAllowance:
    def __init__(self, path: Path, *, limit: int, case_limits: dict, historical=()):
        self.path, self.limit, self.case_limits = path, limit, case_limits
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.locked():
            if path.exists():
                data = json.loads(path.read_bytes())
                if data["limit"] != limit or data["case_limits"] != case_limits:
                    raise ValueError("approved_request_allowance_changed")
            else:
                self.write(
                    {
                        "schema_version": "request-allowance/0.1",
                        "limit": limit,
                        "case_limits": case_limits,
                        "entries": list(historical),
                    }
                )

    @contextmanager
    def locked(self):
        lock = self.path.with_suffix(".lock")
        # Never guess that an existing reservation lock is stale and silently reset the cap.
        with lock.open("x", encoding="utf-8") as stream:
            stream.write("single local experiment writer\n")
        try:
            yield
        finally:
            lock.unlink()

    def write(self, data):
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(self.path)

    def completion_remaining(self, case_id, method):
        data = self.snapshot()
        used = 0
        for entry in data["entries"]:
            if entry["case_id"] != case_id or entry["method"] != method:
                continue
            usage = entry.get("usage")
            count = usage.get("completion_tokens") if isinstance(usage, dict) else None
            used += count if type(count) is int and count >= 0 else entry.get("max_tokens", 2000)
        return max(0, 4000 - used)

    def reserve(self, case_id, method, fingerprint, *, max_tokens=0):
        with self.locked():
            data = json.loads(self.path.read_bytes())
            if case_id not in self.case_limits or method not in self.case_limits[case_id]:
                raise ModelCallFailed("outside_approved_experiment")
            used = sum(e["case_id"] == case_id and e["method"] == method for e in data["entries"])
            if len(data["entries"]) >= self.limit or used >= self.case_limits[case_id][method]:
                raise ModelCallFailed("approved_request_budget_exhausted")
            consumed = 0
            for entry in data["entries"]:
                if entry["case_id"] == case_id and entry["method"] == method:
                    usage = entry.get("usage")
                    value = usage.get("completion_tokens") if isinstance(usage, dict) else None
                    consumed += (
                        value
                        if type(value) is int and value >= 0
                        else entry.get("max_tokens", 2000)
                    )
            if consumed + max_tokens > 4000:
                raise ModelCallFailed("approved_request_budget_exhausted")
            sequence = len(data["entries"]) + 1
            data["entries"].append(
                {
                    "sequence": sequence,
                    "case_id": case_id,
                    "method": method,
                    "fingerprint": fingerprint,
                    "status": "reserved",
                    "max_tokens": max_tokens,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            self.write(data)
            return sequence

    def finish(self, sequence, *, status, usage=None, error_code=None, artifact=None):
        with self.locked():
            data = json.loads(self.path.read_bytes())
            entry = next(e for e in data["entries"] if e["sequence"] == sequence)
            if entry["status"] != "reserved":
                raise ValueError("request_reservation_already_finished")
            entry.update(status=status, usage=usage, error_code=error_code, artifact=artifact)
            self.write(data)

    def snapshot(self):
        with self.locked():
            data = json.loads(self.path.read_bytes())
        return data | {
            "requests_reserved": len(data["entries"]),
            "remaining": self.limit - len(data["entries"]),
        }

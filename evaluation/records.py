"""Every frozen cell stays in the denominator, including unknown or never-started runs."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


@contextmanager
def exclusive(path):
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("single evaluation writer; inspect interrupted lock before manual recovery\n")
    try:
        yield
    finally:
        lock.unlink()


def atomic_json(path, value):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


class RunRecords:
    def __init__(self, root, identity, body, *, mode):
        self.root, self.identity, self.body, self.mode = root, identity, body, mode
        self.index = root / "runs.json"
        self.store = ArtifactStore(root)
        with exclusive(self.index):
            if self.index.exists():
                self.snapshot()
            else:
                atomic_json(
                    self.index,
                    {
                        "experiment_id": identity["experiment_id"],
                        "mode": mode,
                        "rows": {
                            c["cell_id"]: c | {"state": "not_started", "record": None}
                            for c in body["cells"]
                        },
                    },
                )

    def snapshot(self):
        value = json.loads(self.index.read_bytes())
        if value["experiment_id"] != self.identity["experiment_id"] or value["mode"] != self.mode:
            raise ValueError("run_directory_belongs_to_another_experiment_or_mode")
        if set(value["rows"]) != {c["cell_id"] for c in self.body["cells"]}:
            raise ValueError("experiment_denominator_changed")
        for cell in self.body["cells"]:
            row = value["rows"][cell["cell_id"]]
            if any(row[k] != v for k, v in cell.items()):
                raise ValueError("experiment_cell_identity_changed")
            if row["record"]:
                saved = self.store.read_json(row["record"]["sha256"])
                if saved["cell"] != cell or saved["state"] != row["state"]:
                    raise ValueError("run_record_changed")
        return value

    def claim(self, cell_id):
        with exclusive(self.index):
            data = self.snapshot()
            if data["rows"][cell_id]["state"] != "not_started":
                return False
            data["rows"][cell_id]["state"] = "running"
            atomic_json(self.index, data)
            return True

    def finish(self, cell, observation):
        with exclusive(self.index):
            data = self.snapshot()
            if data["rows"][cell["cell_id"]]["state"] != "running":
                raise ValueError("only_running_cells_can_finish")
            if observation["state"] not in {"completed", "guarded", "failed", "unknown"}:
                raise ValueError("invalid_evaluation_terminal_state")
            saved = {"cell": cell, "observed_at": datetime.now(UTC).isoformat(), **observation}
            ref = self.store.put_json(saved)
            data["rows"][cell["cell_id"]] |= {"state": observation["state"], "record": ref}
            atomic_json(self.index, data)
            return saved


class ProviderLedger:
    """One global cap and one root-cell cap; failed/unknown requests still consume both."""

    def __init__(self, path: Path, *, experiment_id, cells, identity):
        self.path = path
        self.lock = RLock()
        self.spec = {
            "experiment_id": experiment_id,
            "cells": cells,
            "model_identity": identity,
            "maximum_requests": sum(cells.values()),
            "output_limit_per_cell": 4000,
        }
        with exclusive(path):
            if path.exists():
                if json.loads(path.read_bytes())["spec"] != self.spec:
                    raise ValueError("provider_allowance_changed")
            else:
                atomic_json(path, {"spec": self.spec, "entries": []})

    def reserve(self, cell, fingerprint, max_tokens):
        with self.lock, exclusive(self.path):
            data = json.loads(self.path.read_bytes())
            if data["spec"] != self.spec:
                raise ValueError("provider_allowance_tampered")
            entries = [e for e in data["entries"] if e["cell"] == cell]
            if (
                cell not in self.spec["cells"]
                or len(entries) >= self.spec["cells"][cell]
                or len(data["entries"]) >= self.spec["maximum_requests"]
            ):
                raise ValueError("provider_request_allowance_exhausted")
            spent = 0
            for entry in entries:
                usage = entry.get("usage") or {}
                value = usage.get("completion_tokens")
                spent += value if type(value) is int and value >= 0 else entry["max_tokens"]
            if type(max_tokens) is not int or max_tokens < 1 or spent + max_tokens > 4000:
                raise ValueError("provider_output_allowance_exhausted")
            ticket = len(data["entries"])
            data["entries"].append(
                {
                    "ticket": ticket,
                    "cell": cell,
                    "fingerprint": fingerprint,
                    "max_tokens": max_tokens,
                    "state": "reserved",
                }
            )
            atomic_json(self.path, data)
            return ticket

    def finish(self, ticket, *, artifact, usage, state):
        if state not in {"completed", "failed"}:
            raise ValueError("invalid_provider_terminal_state")
        with self.lock, exclusive(self.path):
            data = json.loads(self.path.read_bytes())
            if data["spec"] != self.spec or data["entries"][ticket]["state"] != "reserved":
                raise ValueError("provider_reservation_changed")
            data["entries"][ticket].update(artifact=artifact, usage=usage, state=state)
            atomic_json(self.path, data)


class MeteredModel:
    def __init__(self, delegate, ledger, cell, store):
        self.delegate, self.identity = delegate, delegate.identity
        self.ledger, self.cell, self.store = ledger, cell, store

    def generate(self, system, payload, *, max_tokens):
        from models.chat_api import ModelCallFailed

        forbidden = {
            "required_claims",
            "acceptable_dispositions",
            "evidence_sets",
            "reference_claim_id",
            "reference_sha256",
            "formal_semantic_metrics",
            "reviewer_id",
            "reviewer_kind",
            "api_key",
            "deepseek_api_key",
            "recall_model_api_key",
            "bearer_token",
            "permit_sha256",
            "secret",
        }

        def inspect(value):
            if isinstance(value, dict):
                if any(str(k).lower() in forbidden for k in value):
                    raise ModelCallFailed("evaluation_label_or_secret_field_in_generation_input")
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)

        inspect(payload)
        value = {
            "system": system,
            "payload": payload,
            "max_tokens": max_tokens,
            "identity": self.identity,
        }
        input_ref = self.store.put_json(value)
        ticket = self.ledger.reserve(self.cell, content_hash(value), max_tokens)
        try:
            result = self.delegate.generate(system, payload, max_tokens=max_tokens)
        except ModelCallFailed as error:
            ref = self.store.put_json(
                {
                    "input_artifact": input_ref,
                    "error_code": error.code,
                    "diagnostics": error.diagnostics,
                }
            )
            self.ledger.finish(
                ticket, artifact=ref, usage=(error.diagnostics or {}).get("usage"), state="failed"
            )
            raise
        ref = self.store.put_json({"input_artifact": input_ref, "result": result})
        self.ledger.finish(ticket, artifact=ref, usage=result.get("usage"), state="completed")
        return result

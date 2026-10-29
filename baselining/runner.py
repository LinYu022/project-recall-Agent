"""Ordinary task execution with either a deliberate preview stop or exact new approval."""

import os
import secrets
import socket
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import URL

from baselining.preparation import checked
from evaluation.executor import CellExecutor
from evaluation.records import MeteredModel, ProviderLedger, RunRecords, exclusive
from evaluation.session import session
from ingestion.records import content_hash
from models.chat_api import ChatJSONModel, ModelCallFailed
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from reviewing.corpus import public_packet, read, write_new
from runtime.artifacts import ArtifactStore

APPROVAL = Path(".local/P1-16/development-baseline-approval.json")
LEDGER = Path(".local/P1-16/development-baseline-provider-allowance.json")


def check_approval(pointer, plan, approval):
    # A file or command flag cannot grant permission; archive an actual user reply first.
    if (
        approval.get("status") != "explicit_user_approved"
        or approval.get("plan_id") != pointer["plan_id"]
        or approval.get("maximum_requests") != plan["design"]["maximum_requests"]
        or approval.get("model") != plan["design"]["model"]
        or approval.get("endpoint") != plan["design"]["endpoint"]
        or approval.get("source_kind") != "actual_human_reply"
        or not approval.get("human_reply")
    ):
        raise ValueError("new_development_pilot_has_no_matching_actual_user_approval")


class PreviewStop:
    identity = {"model_id": "deliberate-stop-before-provider/0.1", "is_simulated": True}

    def __init__(self, store):
        self.store, self.inputs = store, []

    def generate(self, system, payload, *, max_tokens):
        value = {
            "system": system,
            "payload": payload,
            "max_tokens": max_tokens,
            "identity": self.identity,
        }
        self.inputs.append(self.store.put_json(value))
        raise ModelCallFailed("development_preview_stopped_before_provider")


@contextmanager
def postgres(bin_dir, plan_id, mode):
    root = Path(".local/b16db") / plan_id[:12] / (mode[0] + uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    data = (root / "data").resolve()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = secrets.token_urlsafe(32)
    pwfile = root / "initial-password.txt"
    pwfile.write_text(password, encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    suffix = ".exe" if os.name == "nt" else ""

    def command(name, *args, **kwargs):
        return subprocess.run(
            [str(bin_dir.resolve() / (name + suffix)), *map(str, args)],
            check=True,
            creationflags=flags,
            **kwargs,
        )

    try:
        try:
            with (root / "initdb.log").open("w", encoding="utf-8") as log:
                command(
                    "initdb",
                    "-D",
                    data,
                    "-U",
                    "recall_baseline",
                    "--auth=scram-sha-256",
                    "--pwfile",
                    pwfile.resolve(),
                    "--encoding=UTF8",
                    "--locale=C",
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        finally:
            pwfile.unlink(missing_ok=True)
        command(
            "pg_ctl",
            "-D",
            data,
            "-l",
            (root / "server.log").resolve(),
            "-o",
            f"-h 127.0.0.1 -p {port}",
            "-w",
            "-t",
            "30",
            "start",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = URL.create(
            "postgresql+psycopg",
            username="recall_baseline",
            password=password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        )
        yield url, root
    finally:
        if (data / "postmaster.pid").exists():
            command(
                "pg_ctl",
                "-D",
                data,
                "-m",
                "fast",
                "-w",
                "-t",
                "30",
                "stop",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        write_new(root / "stopped.json", {"server_stopped": True, "data_retained_for_audit": True})


def run(pointer, bin_dir, *, mode="preview"):
    if mode not in {"preview", "live"}:
        raise ValueError("unknown_pilot_mode")
    plan, protocol, cells = checked(pointer)
    delegate = ledger = None
    if mode == "live":
        if not APPROVAL.exists():
            raise ValueError("new_development_pilot_requires_explicit_approval")
        check_approval(pointer, plan, read(APPROVAL))
        delegate = ChatJSONModel()
        if (
            delegate.model_id != plan["design"]["model"]
            or delegate.base != plan["design"]["endpoint"]
        ):
            raise ValueError("pilot_model_configuration_outside_approval")
        limits = {
            "fixed:" + c["case_id"] + ":1": c["maximum_requests"] for c in plan["design"]["cases"]
        }
        ledger = ProviderLedger(
            LEDGER, experiment_id=pointer["plan_id"], cells=limits, identity=delegate.identity
        )
    root = Path(".local/b16") / pointer["plan_id"][:12] / mode[0]
    root.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "provider")
    identity = {"experiment_id": pointer["plan_id"]}
    records = RunRecords(root, identity, {"cells": cells}, mode=mode)
    previews = {}

    def factory(cell):
        if mode == "preview":
            model = PreviewStop(store)
            previews[cell["cell_id"]] = model
            return model
        return MeteredModel(delegate, ledger, cell["cell_id"], store)

    with exclusive(root / "writer.json"), postgres(bin_dir, pointer["plan_id"], mode) as (url, db):
        with session(url, pointer["plan_id"], mode="scripted" if mode == "preview" else "live") as (
            engine,
            _,
        ):
            catalog = Catalog(engine, BundleStore(Path(".local/releases")))
            catalog.publish(
                PublishRequest(
                    release_id=protocol.dataset_release_id,
                    manifest_sha256=plan["manifest_sha256"],
                    expected_default=None,
                    retain_until=datetime.now(UTC) + timedelta(days=30),
                )
            )
            executor = CellExecutor(
                catalog,
                root / "runs",
                protocol,
                {},
                model_factory=factory,
                label_root=Path(pointer["directory"]),
            )
            for cell in cells:
                if not records.claim(cell["cell_id"]):
                    continue
                observation = executor.run(cell)
                preview_refs = previews[cell["cell_id"]].inputs if mode == "preview" else []
                observation["preview_inputs"] = preview_refs
                records.finish(cell, observation)
    snapshot = records.snapshot()
    outputs = []
    for cell in cells:
        row = snapshot["rows"][cell["cell_id"]]
        if not row["record"]:
            outputs.append({"cell_id": cell["cell_id"], "state": row["state"], "observed": False})
            continue
        obs = records.store.read_json(row["record"]["sha256"])
        report = None
        if obs.get("report_reference"):
            ref = obs["report_reference"]
            report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                ref["report_artifact"]["sha256"]
            )
        inputs = {r["sha256"]: store.read_json(r["sha256"]) for r in obs["preview_inputs"]}
        if mode == "live":
            for entry in read(LEDGER)["entries"]:
                if entry["cell"] == cell["cell_id"] and entry.get("artifact"):
                    result = store.read_json(entry["artifact"]["sha256"])
                    r = result["input_artifact"]
                    inputs[r["sha256"]] = store.read_json(r["sha256"])
        case = next(c for c in protocol.cases if c.case_id == cell["case_id"])
        packet = public_packet(
            {"observation": obs, "report": report, "case": case, "inputs": inputs},
            pointer["plan_id"],
        )
        write_new(root / "review-packets" / (packet["blind_id"] + ".json"), packet)
        design_case = next(c for c in plan["design"]["cases"] if c["case_id"] == case.case_id)
        reached = (
            "stopped_before_provider"
            if inputs and mode == "preview"
            else ("guarded" if obs["state"] == "guarded" else "program_answer")
        )
        if not inputs and obs["state"] not in {"guarded", "completed"}:
            reached = "failed_before_provider"
        expected = design_case["expected_preview"]
        disposition = (report or {}).get("proposal") or {}
        outputs.append(
            {
                "cell_id": cell["cell_id"],
                "state": row["state"],
                "preview_model_inputs": len(inputs) if mode == "preview" else None,
                "expected_preview": expected,
                "actual_preview": reached if mode == "preview" else None,
                "preview_expectation_met": (
                    reached == expected
                    and (
                        reached != "program_answer"
                        or disposition.get("disposition") == design_case["expected_disposition"]
                    )
                )
                if mode == "preview"
                else None,
                "disposition": disposition.get("disposition"),
                "blind_id": packet["blind_id"],
                "observation_sha256": content_hash(obs),
                "formal_semantic_metrics": None,
            }
        )
    result = {
        "schema_version": "development-baseline-run/0.1",
        "plan_id": pointer["plan_id"],
        "mode": mode,
        "root": root.as_posix(),
        "database_root": db.as_posix(),
        "server_stopped": read(db / "stopped.json")["server_stopped"],
        "classification": "real_local_tools_deliberate_model_stop_not_quality_experiment"
        if mode == "preview"
        else "real_provider_development_pilot_pending_independent_review",
        "rows": outputs,
        "denominator": len(cells),
        "new_provider_requests": 0 if mode == "preview" else len(read(LEDGER)["entries"]),
        "population_quality": None,
        "quality_target": None,
    }
    # Run-level database identity changes on resume, so retain each summary by content hash.
    write_new(root / ("summary-" + content_hash(result) + ".json"), result)
    return result

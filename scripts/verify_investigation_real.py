"""Disposable PostgreSQL + frozen official sources. External generation requires --live-model."""

import argparse
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import sqlalchemy as sa
import uvicorn
from alembic import command
from alembic.config import Config

from app.release_api import create_release_app
from ingestion.records import content_hash
from investigation.contracts import FixedConfig, ReferenceCase
from investigation.flow import FixedInvestigator
from investigation.review import export_blind
from investigation.scoring import score
from models.chat_api import ChatJSONModel, ModelCallFailed
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import BindTask, PublishRequest
from releases.service import PublishedService
from runtime.artifacts import ArtifactStore


class RecordedModel:
    """Explicit cached-response replay; never silently spend money on a cache miss."""

    def __init__(self, delegate, root, *, live=False, replay=False, reuse=False, allowance=None):
        self.delegate, self.identity = delegate, delegate.identity
        self.store = ArtifactStore(root)
        self.live, self.replay = live, replay
        self.reuse = reuse
        self.allowance = allowance
        self.current_case = self.current_method = None
        self.calls, self.replays = 0, 0
        self.mapping = root / "mapping.json"
        self.entries = json.loads(self.mapping.read_bytes()) if self.mapping.exists() else {}
        self.error_mapping = root / "errors.json"
        self.errors = (
            json.loads(self.error_mapping.read_bytes()) if self.error_mapping.exists() else {}
        )

    def generate(self, system, payload, *, max_tokens):
        fingerprint = content_hash(
            {
                "system": system,
                "payload": payload,
                "max_tokens": max_tokens,
                "identity": self.identity,
            }
        )
        if self.replay or self.reuse:
            if fingerprint in self.errors:
                failure = self.store.read_json(self.errors[fingerprint])
                self.replays += 1
                raise ModelCallFailed(
                    failure["error_code"], (failure.get("diagnostics") or {}) | {"replayed": True}
                )
            if fingerprint not in self.entries and self.replay:
                raise ModelCallFailed("recorded_response_missing")
            if fingerprint in self.entries:
                self.replays += 1
                return self.store.read_json(self.entries[fingerprint]) | {"replayed": True}
        if not self.live:
            raise ModelCallFailed("live_model_not_enabled")
        ticket = (
            self.allowance.reserve(
                self.current_case, self.current_method, fingerprint, max_tokens=max_tokens
            )
            if self.allowance
            else None
        )
        self.calls += 1
        try:
            result = self.delegate.generate(system, payload, max_tokens=max_tokens)
        except ModelCallFailed as error:
            ref = self.store.put_json({"error_code": error.code, "diagnostics": error.diagnostics})
            self.errors[fingerprint] = ref["sha256"]
            self.error_mapping.write_text(
                json.dumps(self.errors, indent=2) + "\n", encoding="utf-8"
            )
            if self.allowance:
                self.allowance.finish(
                    ticket,
                    status="failed",
                    usage=(error.diagnostics or {}).get("usage"),
                    error_code=error.code,
                    artifact=ref,
                )
            raise
        ref = self.store.put_json(result)
        self.entries[fingerprint] = ref["sha256"]
        self.mapping.write_text(json.dumps(self.entries, indent=2) + "\n", encoding="utf-8")
        if self.allowance:
            self.allowance.finish(
                ticket, status="completed", usage=result.get("usage"), artifact=ref
            )
        return result


class SyntheticModel:
    identity = {"model_id": "synthetic-abstention", "is_simulated": True}

    def generate(self, system, payload, *, max_tokens):
        return {
            "proposal": {
                "disposition": "insufficient_evidence",
                "claims": [],
                "unresolved_fields": ["source_coverage"],
            },
            "accepted_complete": True,
            "usage": None,
            "elapsed_ms": 0,
            "estimated_cost": None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Make at most four paid generation requests, no retries",
    )
    parser.add_argument(
        "--replay-model",
        action="store_true",
        help="Reuse exact saved API responses, no network generation",
    )
    parser.add_argument("--output", type=Path, default=Path(".local/P1-07/real-baseline.json"))
    parser.add_argument("--reference-root", type=Path, default=Path("evals/investigation-v0.2"))
    parser.add_argument(
        "--reuse-recorded",
        action="store_true",
        help="With --live-model, replay exact cached inputs and call only on missing inputs",
    )
    args = parser.parse_args()
    if args.live_model and args.replay_model:
        parser.error("choose live OR replay")
    if args.reuse_recorded and not args.live_model:
        parser.error("--reuse-recorded requires explicit --live-model")
    plan = json.loads(Path("evals/investigation-v0.1/cases.json").read_bytes())
    data = json.loads(Path("examples/release-candidates.json").read_bytes())
    candidate = data["candidates"]["expanded"]
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    schema = "investigation_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = server = listener = thread = None
    root = Path(".local/P1-07")
    model = (
        RecordedModel(
            ChatJSONModel(),
            root / "recorded-model",
            live=args.live_model,
            replay=args.replay_model,
            reuse=args.reuse_recorded,
        )
        if (args.live_model or args.replay_model)
        else SyntheticModel()
    )
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_postgresql_saved_official_sources_"
        + (
            "live_deepseek"
            if args.live_model
            else "recorded_api_replay"
            if args.replay_model
            else "synthetic_generation"
        ),
        "model_identity": model.identity,
        "config": plan["config"],
        "cases": [],
        "http_checks": [],
        "label_author": "implementation_agent",
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "new_source_requests": 0,
    }
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": f"-csearch_path={schema}"},
        )
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        catalog = Catalog(engine, BundleStore(Path(data["root"])))
        catalog.publish(
            PublishRequest(
                **{k: candidate[k] for k in ("release_id", "manifest_sha256")},
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        service = PublishedService(catalog, root / "retrieval-traces")
        investigator = FixedInvestigator(
            service, model, root / "runs", FixedConfig.model_validate(plan["config"])
        )
        for case in plan["cases"]:
            catalog.bind(
                BindTask(
                    task_id=case["case_id"], allowed_source_ids=case["sources"], scope=case["scope"]
                )
            )
        app = create_release_app(development_mode=True, service=service, investigator=investigator)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=180, trust_env=False
        ) as client:
            for case in plan["cases"]:
                response = client.post("/published/investigate", json=case["request"])
                assert response.status_code == 200, (case["case_id"], response.status_code)
                result = response.json()
                saved = ArtifactStore(Path(result["artifact_root"])).read_json(
                    result["report_artifact"]["sha256"]
                )
                path = root / "reports" / (case["case_id"] + ".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                reference = ReferenceCase.model_validate_json(
                    (args.reference_root / (case["case_id"] + ".json")).read_bytes()
                )
                scoring = score(saved, reference)
                scorepath = root / "scores" / (case["case_id"] + ".json")
                scorepath.parent.mkdir(parents=True, exist_ok=True)
                scorepath.write_text(
                    json.dumps(scoring, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                blind = export_blind(saved, root / "blind")
                entry = {
                    "case_id": case["case_id"],
                    "execution_status": saved["execution_status"],
                    "disposition": saved["proposal"]["disposition"] if saved["proposal"] else None,
                    "generation_mode": saved["generation_mode"],
                    "error_code": saved["error_code"],
                    "tool_calls": len(saved["tool_calls"]),
                    "model_calls": saved["model_calls"],
                    "usage": saved.get("model_usage"),
                    "estimated_cost": saved.get("estimated_cost"),
                    "elapsed_ms": saved["total_elapsed_ms"],
                    "report_artifact": result["report_artifact"],
                    "report_content_sha256": content_hash(saved),
                    "report_path": path.as_posix(),
                    "blind_id": blind,
                    "score": scoring,
                }
                record["cases"].append(entry)
                print(
                    json.dumps(
                        {
                            k: entry[k]
                            for k in (
                                "case_id",
                                "execution_status",
                                "disposition",
                                "generation_mode",
                                "error_code",
                            )
                        }
                    ),
                    flush=True,
                )
            first = plan["cases"][0]["request"]
            for name, payload, expected in [
                ("scope_override", first | {"scope": {}}, 422),
                ("unbound_task", first | {"task_id": "NOT-BOUND"}, 409),
                (
                    "mixed_release",
                    first | {"expected_release_id": data["candidates"]["baseline"]["release_id"]},
                    409,
                ),
            ]:
                response = client.post("/published/investigate", json=payload)
                assert response.status_code == expected
                record["http_checks"].append({"case": name, "status": response.status_code})
            pair = {
                "request": {"task_id": "DEV-SCOPE-EXCLUSION", "query": "inspect"},
                **plan["cases"][3]["request"]["pairs"][0],
            }
            inspected = client.post("/published/entity-links", json=pair).json()
            assert inspected["endpoint_scope_channels"] == ["excluded", "matched"]
            record["http_checks"].append(
                {
                    "case": "known_exclusion_inspection",
                    "channels": inspected["endpoint_scope_channels"],
                }
            )
        for identifier, disposition in [
            ("DEV-ZERO", "no_matching_records"),
            ("DEV-MISSING", "needs_clarification"),
            ("DEV-TIME", "unsupported_time"),
            ("DEV-INSUFFICIENT", "insufficient_evidence"),
        ]:
            entry = next(c for c in record["cases"] if c["case_id"] == identifier)
            assert entry["disposition"] == disposition and entry["model_calls"] == 0
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=15)
            assert not thread.is_alive()
        if listener:
            listener.close()
        if engine:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()
        record.update(
            http_server_stopped=True,
            disposable_schema_removed=True,
            external_generative_model_calls=getattr(model, "calls", 0),
            recorded_response_replays=getattr(model, "replays", 0),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return int(any(c["execution_status"] != "completed" for c in record["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())

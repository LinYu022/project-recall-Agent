"""Paired fixed/agent runs on saved official sources, with explicit live/replay generation."""

import argparse
import json
import os
import secrets
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

from app.control_api import create_control_app
from control.service import TaskControl
from ingestion.records import content_hash
from investigation.contracts import FixedConfig, ReferenceCase
from investigation.review import export_blind
from investigation.scoring import score
from models.chat_api import ChatJSONModel, ModelCallFailed
from models.request_allowance import RequestAllowance
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService
from runtime.artifacts import ArtifactStore
from scripts.verify_investigation_real import RecordedModel, SyntheticModel


class SyntheticDecisionModel(SyntheticModel):
    identity = {"model_id": "scripted-official-source-loop", "is_simulated": True}

    def generate(self, system, payload, *, max_tokens):
        response = super().generate(system, payload, max_tokens=max_tokens)
        if "available_tools" in payload:
            action, query, document = "finish", None, None
            text_sources = {
                s
                for e in payload["evidence"]
                if e["kind"] == "text"
                for s in e["payload"].get("source_ids", [])
            }
            if (
                payload.get("pinned_pairs")
                and not any(e["kind"] == "relationship" for e in payload["evidence"])
                and "links" in payload["available_tools"]
            ):
                action = "links"
            elif not payload["candidates"] and "search" in payload["available_tools"]:
                action, query = "search", payload["initial_query"]
            elif "expand" in payload["available_tools"]:
                candidate = next(
                    (c for c in payload["candidates"] if c["source_id"] not in text_sources), None
                )
                if candidate:
                    action, document = "expand", candidate["id"]
            response["proposal"] = {
                "action": action,
                "reason": "SYNTHETIC abstention validates the protected loop, not answer quality",
                "hypotheses": [],
                "evidence_gaps": ["synthetic generation is not a real quality experiment"],
                "next_steps": [],
                "query": query,
                "document_id": document,
                "include_related": False if action == "expand" else None,
                "proposal": response["proposal"] if action == "finish" else None,
            }
        response["usage"] = {"prompt_tokens": 0, "completion_tokens": 40, "total_tokens": 40}
        response["usage_is_synthetic"] = True
        return response


class PortableRunModel:
    """Replace only opaque task/revision identifiers before BOTH live generation and replay.

    Source identities, text, scope, parameters and tool observations are untouched. Mapping
    unguessable administrative IDs is not an input change applied only to a cache lookup.
    """

    def __init__(self, delegate):
        self.delegate, self.identity = delegate, delegate.identity
        self.inputs = ArtifactStore(Path(".local/P1-09/portable-model-inputs"))

    def generate(self, system, payload, *, max_tokens):
        payload = json.loads(json.dumps(payload, ensure_ascii=False))

        def visit(value, key=""):
            if isinstance(value, dict):
                return {k: visit(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [visit(v, key) for v in value]
            if key == "task_id" and isinstance(value, str) and value.startswith("CT-"):
                return "PINNED-TASK"
            if key == "control_revision_id":
                return "PINNED-REVISION"
            return value

        normalized = visit(payload)
        ref = self.inputs.put_json(
            {
                "system": system,
                "payload": normalized,
                "max_tokens": max_tokens,
                "transform": "portable-administrative-identifiers/0.1",
            }
        )
        metadata = {
            "provider_input_artifact": ref,
            "provider_input_root": ".local/P1-09/portable-model-inputs",
        }
        try:
            return self.delegate.generate(system, normalized, max_tokens=max_tokens) | metadata
        except ModelCallFailed as error:
            error.diagnostics = (error.diagnostics or {}) | metadata
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--replay-model", action="store_true")
    parser.add_argument("--reuse-recorded", action="store_true")
    parser.add_argument("--model-call-cap", type=int, default=6)
    parser.add_argument(
        "--profile-record",
        type=Path,
        help="For replay, pin each effective configuration and expected outcome from this prior record",
    )
    parser.add_argument("--output", type=Path, default=Path(".local/P1-09/comparison.json"))
    args = parser.parse_args()
    if args.live_model and args.replay_model:
        parser.error("choose live or replay")
    if args.reuse_recorded and not args.live_model:
        parser.error("reuse-recorded requires live-model")
    if args.profile_record and not args.replay_model:
        parser.error("profile-record is for recorded-response replay only")
    plan = json.loads(Path("examples/single-agent-comparison.json").read_bytes())
    cases = json.loads(Path("evals/investigation-v0.1/cases.json").read_bytes())["cases"]
    cases = [c for c in cases if c["case_id"] in plan["case_ids"]]
    config = FixedConfig.model_validate(plan["profile"])
    if not 1 <= args.model_call_cap <= 6:
        parser.error("model-call-cap must stay within the frozen upper envelope")
    config = config.model_copy(update={"max_model_calls": args.model_call_cap})
    plan["effective_profile"] = config.model_dump(mode="json")
    candidates = json.loads(Path("examples/release-candidates.json").read_bytes())
    candidate = candidates["candidates"]["expanded"]
    root = Path(".local/P1-09")
    prior_runs = {}
    prior_configs = {}
    if args.profile_record:
        prior = json.loads(args.profile_record.read_bytes())
        prior = prior.get("single_agent", prior)
        for run in prior["runs"]:
            key = (run["case_id"], run["method"])
            prior_runs[key] = run
            # reports/ is overwritten by later runs; pin the immutable original artifact.
            digest = run["report_artifact"]["sha256"]
            matches = list((root / "runs/private-runs").rglob(digest + ".json"))
            if len(matches) != 1:
                raise ValueError("recorded_profile_report_missing_or_ambiguous")
            archived = ArtifactStore(matches[0].parent.parent).read_json(digest)
            if content_hash(archived) != run["report_content_sha256"]:
                raise ValueError("recorded_profile_report_hash_mismatch")
            prior_configs[key] = archived["config"]
    allowance = None
    if args.live_model:
        history = []
        prior = root / "live-round1/live-comparison.json"
        if prior.exists():
            original = json.loads(prior.read_bytes())["single_agent"]
            for run in original["runs"]:
                if run["model_calls"] != 1:
                    raise ValueError("historical_run_requires_explicit_request_accounting")
                history.append(
                    {
                        "sequence": len(history) + 1,
                        "case_id": run["case_id"],
                        "method": run["method"],
                        "status": "historical_recorded",
                        "source_report_sha256": run["report_content_sha256"],
                        "usage": run.get("resources", {}).get("provider_usage_totals"),
                        "raw_response_available": run["error_code"] != "model_content_invalid_json",
                    }
                )
            if len(history) != original["external_model_calls"]:
                raise ValueError("historical_model_request_count_mismatch")
        allowance = RequestAllowance(
            root / "request-allowance.json",
            limit=14,
            case_limits={case["case_id"]: {"fixed": 1, "single_agent": 6} for case in cases},
            historical=history,
        )
    recorded = (
        RecordedModel(
            ChatJSONModel(),
            root / "recorded-model",
            live=args.live_model,
            replay=args.replay_model,
            reuse=args.reuse_recorded,
            allowance=allowance,
        )
        if (args.live_model or args.replay_model)
        else SyntheticDecisionModel()
    )
    model = PortableRunModel(recorded)
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    schema = "single_agent_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = server = listener = thread = None
    result = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "saved_official_sources_real_postgresql_protected_http_"
        + (
            "live_api"
            if args.live_model
            else "recorded_api_replay"
            if args.replay_model
            else "synthetic_generation"
        ),
        "profile": plan,
        "release_id": candidate["release_id"],
        "model_identity": model.identity,
        "runs": [],
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
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        catalog = Catalog(engine, BundleStore(Path(candidates["root"])))
        catalog.publish(
            PublishRequest(
                **{k: candidate[k] for k in ("release_id", "manifest_sha256")},
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        published = PublishedService(catalog, root / "retrieval-traces")
        control = TaskControl(published, model, root / "runs", config=config)
        credential = secrets.token_urlsafe(32)
        app = create_control_app(control, {credential: "comparison-user"})
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
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            timeout=1200,
            trust_env=False,
            headers={"Authorization": "Bearer " + credential},
        ) as client:
            for case in cases:
                reference = ReferenceCase.model_validate_json(
                    (Path(plan["label_root"]) / (case["case_id"] + ".json")).read_bytes()
                )
                for method in ["fixed", "single_agent"]:
                    if isinstance(recorded, RecordedModel):
                        recorded.current_case, recorded.current_method = case["case_id"], method
                    control.config = config
                    if prior_configs:
                        control.config = FixedConfig.model_validate(
                            prior_configs[(case["case_id"], method)]
                        )
                    if allowance and method == "single_agent":
                        remaining = allowance.completion_remaining(case["case_id"], method)
                        if remaining < 256:
                            raise ValueError("approved_case_completion_budget_unavailable")
                        control.config = config.model_copy(
                            update={
                                "total_generation_tokens": min(
                                    config.total_generation_tokens, remaining
                                )
                            }
                        )
                    task_id = case["case_id"] + "-" + ("FIXED" if method == "fixed" else "AGENT")
                    request = case["request"] | {"task_id": task_id}
                    task = {
                        "task_id": task_id,
                        "plan": {
                            "release_id": candidate["release_id"],
                            "allowed_source_ids": case["sources"],
                            "scope": case["scope"],
                            "request": request,
                            "mode": "EXECUTE",
                            "investigator": method,
                            "hypotheses": [],
                            "steps": [],
                        },
                    }
                    response = client.post("/tasks", json=task)
                    assert response.status_code == 200, (task_id, response.status_code)
                    ready = response.json()
                    response = client.post(
                        "/tasks/" + task_id + "/execute",
                        json={"expected_version": ready["version"]},
                    )
                    assert response.status_code == 200, (task_id, response.status_code)
                    state = response.json()
                    history = control.history("comparison-user", task_id)
                    execution = history["executions"][-1]
                    # The protected report endpoint exposes completed deliveries only. Failed
                    # runs are inspected here by the trusted experiment operator from private artifacts.
                    with engine.connect() as connection:
                        from control.schema import executions

                        private = connection.scalar(
                            sa.select(executions.c.private_report).where(
                                executions.c.execution_id == execution["execution_id"]
                            )
                        )
                    if private:
                        report = ArtifactStore(Path(private["artifact_root"])).read_json(
                            private["report_artifact"]["sha256"]
                        )
                        target = root / "reports" / (task_id + ".json")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(
                            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        scoring = score(report, reference)
                        scored = root / "scores" / (task_id + ".json")
                        scored.parent.mkdir(parents=True, exist_ok=True)
                        scored.write_text(
                            json.dumps(scoring, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        blind = export_blind(report, root / "blind")
                        summary = {
                            "case_id": case["case_id"],
                            "method": method,
                            "task_state": state["state"],
                            "execution_status": report["execution_status"],
                            "disposition": (report.get("proposal") or {}).get("disposition"),
                            "generation_mode": report["generation_mode"],
                            "model_calls": report["model_calls"],
                            "tool_calls": len(report["tool_calls"]),
                            "termination": report.get("termination"),
                            "error_code": report.get("error_code"),
                            "resources": report.get("resources"),
                            "elapsed_ms": report["total_elapsed_ms"],
                            "report_content_sha256": content_hash(report),
                            "report_artifact": private["report_artifact"],
                            "report_path": target.as_posix(),
                            "blind_id": blind,
                            "score": scoring,
                        }
                    else:
                        summary = {
                            "case_id": case["case_id"],
                            "method": method,
                            "task_state": state["state"],
                            "execution_status": "failed",
                            "error_code": "no_private_run_report",
                        }
                    result["runs"].append(summary)
                    print(
                        json.dumps(
                            {
                                k: summary.get(k)
                                for k in (
                                    "case_id",
                                    "method",
                                    "execution_status",
                                    "disposition",
                                    "model_calls",
                                    "tool_calls",
                                    "termination",
                                    "error_code",
                                )
                            }
                        ),
                        flush=True,
                    )
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
        result.update(
            http_server_stopped=True,
            schema_removed=True,
            external_model_calls=getattr(recorded, "calls", 0),
            recorded_response_replays=getattr(recorded, "replays", 0),
        )
        if prior_runs:
            keys = (
                "execution_status",
                "error_code",
                "disposition",
                "model_calls",
                "tool_calls",
                "termination",
            )
            result["replay_outcomes_match"] = all(
                all(run.get(k) == prior_runs[(run["case_id"], run["method"])].get(k) for k in keys)
                for run in result["runs"]
            ) and len(result["runs"]) == len(prior_runs)
        if allowance:
            result["approved_request_allowance"] = allowance.snapshot()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if prior_runs:
        return int(not result["replay_outcomes_match"])
    return int(any(r["execution_status"] != "completed" for r in result["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())

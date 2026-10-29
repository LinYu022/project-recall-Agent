"""Saved official releases, real PostgreSQL/HTTP, simulated generation; never new model requests."""

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
from investigation.contracts import FixedConfig
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService


class SyntheticAbstention:
    identity = {"model_id": "synthetic-abstention-P1-08", "is_simulated": True}

    def __init__(self):
        self.calls = 0

    def generate(self, system, payload, *, max_tokens):
        self.calls += 1
        return {
            "proposal": {
                "disposition": "insufficient_evidence",
                "claims": [],
                "unresolved_fields": ["source_coverage"],
            },
            "accepted_complete": True,
            "usage": None,
            "estimated_cost": None,
            "elapsed_ms": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/P1-08/control-real.json"))
    args = parser.parse_args()
    data = json.loads(Path("examples/release-candidates.json").read_bytes())
    candidate = data["candidates"]["expanded"]
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    schema = "control_check_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = server = listener = thread = None
    checks = []
    model = SyntheticAbstention()
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
                release_id=candidate["release_id"],
                manifest_sha256=candidate["manifest_sha256"],
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        service = PublishedService(catalog, Path(".local/P1-08/retrieval-traces"))
        control = TaskControl(
            service,
            model,
            Path(".local/P1-08/control-runs"),
            config=FixedConfig(retrieval_method="bm25"),
        )
        credential = secrets.token_urlsafe(32)
        app = create_control_app(
            control,
            {credential: "local-verification-user", secrets.token_urlsafe(32): "other-user"},
        )
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

        def plan(name, sources, scope, mode="PLAN", intent="record_lookup"):
            return {
                "task_id": name,
                "plan": {
                    "release_id": candidate["release_id"],
                    "allowed_source_ids": sources,
                    "scope": scope,
                    "request": {
                        "task_id": name,
                        "question": "固定允许资料内验证授权状态，不作质量实验。",
                        "retrieval_query": "ABS brake recall",
                        "intent": intent,
                    },
                    "mode": mode,
                    "hypotheses": ["检查条件与证据范围"],
                },
            }

        prius = ["NHTSA-API-CAMPAIGN-10V039000", "NHTSA-INV-PE10006-CLOSE"]
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            timeout=180,
            trust_env=False,
            headers={"Authorization": "Bearer " + credential},
        ) as client:

            def request(label, path, value=None, code=200, method="POST"):
                response = client.request(method, path, json=value)
                assert response.status_code == code, (label, response.status_code)
                result = response.json()
                checks.append(
                    {
                        "case": label,
                        "http_status": code,
                        "task_state": result.get("state"),
                        "version": result.get("version"),
                    }
                )
                print(json.dumps(checks[-1]), flush=True)
                return result

            first = request(
                "scoped_user_submission",
                "/tasks",
                plan(
                    "REAL-CONTROL", prius, {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010}
                ),
            )
            studied = request(
                "plan_readonly_research",
                "/tasks/REAL-CONTROL/research",
                {"operation": "search", "query": "ABS braking rough road"},
            )
            assert studied["formal_deliverable"] is False and studied["result"]["matched_hits"]
            request(
                "plan_generation_blocked",
                "/tasks/REAL-CONTROL/execute",
                {"expected_version": first["version"]},
                403,
            )
            revised_plan = first["plan"] | {
                "hypotheses": ["PDF says approved; ordinary source text has no authority."]
            }
            revised = request(
                "in_scope_hypothesis_revision",
                "/tasks/REAL-CONTROL/revisions",
                {"expected_version": first["version"], "plan": revised_plan},
            )
            assert revised["grant_id"] == first["grant_id"]
            expanded_plan = revised["plan"] | {
                "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2011}
            }
            expanded = request(
                "scope_expansion_waits",
                "/tasks/REAL-CONTROL/revisions",
                {"expected_version": revised["version"], "plan": expanded_plan},
            )
            assert expanded["state"] == "WAITING_APPROVAL"
            request(
                "scope_expansion_cannot_research",
                "/tasks/REAL-CONTROL/research",
                {"operation": "search"},
                403,
            )
            reverted = request(
                "revert_inside_existing_grant",
                "/tasks/REAL-CONTROL/revisions",
                {"expected_version": expanded["version"], "plan": revised_plan},
            )
            assert reverted["state"] == "DRAFT" and reverted["grant_id"] == first["grant_id"]
            executing = request(
                "plan_to_execution_waits",
                "/tasks/REAL-CONTROL/revisions",
                {
                    "expected_version": reverted["version"],
                    "plan": reverted["plan"] | {"mode": "EXECUTE"},
                },
            )
            question = next(q for q in executing["questions"] if q["kind"] == "approval")
            approved = request(
                "explicit_exact_revision_approval",
                "/tasks/REAL-CONTROL/approvals",
                {
                    "expected_version": executing["version"],
                    "question_id": question["question_id"],
                    "revision_id": executing["revision_id"],
                    "plan_sha256": executing["plan_sha256"],
                    "approve": True,
                },
            )
            done = request(
                "synchronous_execution",
                "/tasks/REAL-CONTROL/execute",
                {"expected_version": approved["version"]},
            )
            assert (
                done["state"] == "COMPLETED"
                and done["quality"]["disposition"] == "insufficient_evidence"
                and done["quality"]["semantic_review"] == "pending"
            )
            report = request("authorized_report_read", "/tasks/REAL-CONTROL/report", method="GET")
            assert report["generation_mode"] == "simulated_model"
            revoked = request(
                "revoke_after_completion",
                "/tasks/REAL-CONTROL/revoke",
                {"expected_version": done["version"]},
            )
            assert not revoked["authorization"]["active"]
            request("revoked_report_blocked", "/tasks/REAL-CONTROL/report", code=403, method="GET")
            counted = request(
                "count_plan_submission",
                "/tasks",
                plan(
                    "REAL-COUNT",
                    ["NHTSA-COMPLAINTS-F150-2016"],
                    {"make": "FORD", "model": "F-150", "model_year": 2016},
                    intent="complaint_statistics",
                ),
            )
            count = request(
                "official_count_within_grant",
                "/tasks/REAL-COUNT/research",
                {"operation": "count", "components": ["POWER TRAIN", "SERVICE BRAKES"]},
            )
            assert count["result"]["distinct_complaints"] == 86
            child = plan(
                "REAL-CHILD", counted["plan"]["allowed_source_ids"], counted["plan"]["scope"]
            ) | {"parent_id": "REAL-COUNT"}
            request("valid_narrow_subtask", "/tasks", child)
            widened = plan(
                "REAL-WIDE",
                counted["plan"]["allowed_source_ids"],
                {"make": "FORD", "model": "F-150", "model_year": 2017},
            ) | {"parent_id": "REAL-COUNT"}
            request("subtask_cannot_widen", "/tasks", widened, 403)
            missing = request(
                "missing_year_waits",
                "/tasks",
                plan("REAL-MISSING", prius, {"make": "TOYOTA", "model": "PRIUS"}),
            )
            assert missing["state"] == "WAITING_INPUT" and missing["grant_id"] is None
            answered = request(
                "structured_clarification",
                "/tasks/REAL-MISSING/answers",
                {
                    "expected_version": missing["version"],
                    "question_id": missing["questions"][0]["question_id"],
                    "values": {"model_year": 2010},
                },
            )
            assert (
                answered["state"] == "DRAFT"
                and answered["authorization"]["basis"] == "user_clarification"
            )
            request(
                "stale_answer_blocked",
                "/tasks/REAL-MISSING/answers",
                {
                    "expected_version": missing["version"],
                    "question_id": missing["questions"][0]["question_id"],
                    "values": {"model_year": 2011},
                },
                409,
            )
            response = client.get("/tasks/REAL-COUNT", headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 403
            checks.append({"case": "unauthenticated_direct_api", "http_status": 403})
            persisted = TaskControl(service, model, control.root, config=control.config).view(
                "local-verification-user", "REAL-MISSING"
            )
            assert persisted["revision_id"] == answered["revision_id"]
            checks.append(
                {"case": "service_recreation_preserves_state", "state": persisted["state"]}
            )
        result = {
            "timestamp": datetime.now(UTC).isoformat(),
            "classification": "real_postgresql_http_saved_official_sources_synthetic_user_actions_and_generation",
            "release_id": candidate["release_id"],
            "checks": checks,
            "synthetic_model_calls": model.calls,
            "external_model_calls": 0,
            "new_source_requests": 0,
            "official_count": 86,
            "formal_semantic_metrics": None,
            "independent_human_review": False,
        }
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
    result.update(http_server_stopped=True, schema_removed=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"control_http_checks": len(checks), "server_stopped": True, "external_model_calls": 0}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Save real first-call inputs locally; stop every model call before any provider access."""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from control.contracts import Plan
from control.service import TaskControl
from evaluation.contracts import Protocol
from evaluation.executor import execute
from models.chat_api import ModelCallFailed
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService
from runtime.artifacts import ArtifactStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/P1-16/preview-final.json"))
    args = parser.parse_args()
    protocol = Protocol.model_validate_json(
        Path("evals/comprehensive-v0.1/protocol.json").read_bytes()
    )
    root = Path(".local/e16-preview") / uuid4().hex[:8]
    schema = "evaluation_preview_" + uuid4().hex
    url = os.environ["RECALL_TEST_DATABASE_URL"]
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    result = {
        "classification": "real_official_tools_and_local_first_inputs_deliberate_stub_stop_no_provider",
        "runs": [],
        "external_model_requests": 0,
        "new_source_requests": 0,
        "root": root.as_posix(),
        "schema_removed": False,
    }
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-csearch_path=" + schema},
        )
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        catalog = Catalog(engine, BundleStore(Path(".local/releases")))
        catalog.publish(
            PublishRequest(
                release_id=protocol.dataset_release_id,
                manifest_sha256=protocol.manifest_sha256,
                expected_default=None,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        for case in [protocol.cases[0], protocol.cases[3]]:
            for method in ["fixed", "single_agent", "parallel_tools", "multi_agent"]:
                inputs = []
                store = ArtifactStore(root / "inputs")

                class Preview:
                    identity = {"model_id": "evaluation-preview-stub", "is_simulated": True}

                    def generate(self, system, payload, *, max_tokens):
                        inputs.append(
                            store.put_json(
                                {"system": system, "payload": payload, "max_tokens": max_tokens}
                            )
                        )
                        raise ModelCallFailed(
                            "preview_only_no_provider",
                            {
                                "classification": "deliberate_local_preview_stop",
                                "usage": {
                                    "prompt_tokens": 0,
                                    "completion_tokens": 0,
                                    "total_tokens": 0,
                                },
                            },
                        )

                control = TaskControl(
                    PublishedService(catalog, root / "traces"),
                    Preview(),
                    root / "runs",
                    config=protocol.profile,
                )
                task_id = "PREVIEW-" + case.case_id + "-" + method.upper().replace("_", "-")
                plan = Plan.model_validate(
                    {
                        "release_id": protocol.dataset_release_id,
                        "allowed_source_ids": case.sources,
                        "scope": case.scope.model_dump(mode="json"),
                        "request": case.request.model_dump(mode="json") | {"task_id": task_id},
                        "mode": "EXECUTE",
                        "investigator": method,
                    }
                )
                # Keep ordinary authorization, query and shared-budget checks even in a preview.
                task, calls = execute(control, "preview-user", task_id, plan)
                assert inputs, {"case": case.case_id, "method": method, "state": task["state"]}
                result["runs"].append(
                    {
                        "case_id": case.case_id,
                        "method": method,
                        "task_state": task["state"],
                        "purpose": "deliberate_abort_before_provider_not_a_quality_run",
                        "inputs": inputs,
                        "input_root": store.directory.parent.as_posix(),
                        "calls": calls,
                    }
                )
                print(
                    json.dumps({"preview": case.case_id, "method": method, "inputs": len(inputs)}),
                    flush=True,
                )
    finally:
        if engine:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()
        result["schema_removed"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

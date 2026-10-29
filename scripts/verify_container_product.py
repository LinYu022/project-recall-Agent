"""Build and exercise a fresh synthetic Compose deployment; never calls a model provider."""

import argparse
import hashlib
import json
import secrets
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18018)
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument(
        "--ecr-public-mirror",
        action="store_true",
        help="Use the AWS public Docker Official Images mirror for the same versioned base images",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output.resolve()
    token = secrets.token_hex(24)
    project = "recall-check-" + secrets.token_hex(5)
    env = root / "runtime.env"
    env.write_text(
        "RECALL_DB_PASSWORD="
        + secrets.token_hex(24)
        + "\n"
        + 'RECALL_CONTROL_TOKEN_OWNERS={"'
        + token
        + '":"release-check"}\n',
        encoding="utf-8",
    )
    env.chmod(0o600)
    if args.ecr_public_mirror:
        with env.open("a", encoding="utf-8") as stream:
            stream.write(
                "RECALL_NODE_IMAGE=public.ecr.aws/docker/library/node:22.22.0-bookworm-slim\nRECALL_PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12.14-slim-bookworm\nRECALL_POSTGRES_IMAGE=public.ecr.aws/docker/library/postgres:18.6\n"
            )
    override = root / "ports.yaml"
    override.write_text(
        'services:\n  product:\n    ports: !override ["127.0.0.1:' + str(args.port) + ':8017"]\n',
        encoding="utf-8",
    )
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(env),
        "-f",
        "compose.yaml",
        "-f",
        str(override),
    ]
    checks: list[dict] = []
    commands: list[dict] = []
    base = "http://127.0.0.1:" + str(args.port)
    result = {
        "classification": "fresh_container_synthetic_product_not_model_quality",
        "project": project,
        "external_model_calls": 0,
        "source_downloads": 0,
        "checks": checks,
        "commands": commands,
        "success": False,
        "services_stopped": False,
    }

    def command(name, arguments, timeout=1800):
        print(json.dumps({"stage": name, "status": "running"}), flush=True)
        with (root / (name + ".log")).open("wb") as log:
            completed = subprocess.run(
                [*compose, *arguments],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        commands.append({"stage": name, "exit_code": completed.returncode})
        if completed.returncode:
            raise RuntimeError(name + "_failed_see_local_log")

    def request(path, body=None, credential=token):
        headers = {"Authorization": "Bearer " + credential} if credential else {}
        data = None if body is None else json.dumps(body).encode()
        if data:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(base + path, data=data, headers=headers), timeout=90) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, None

    def ok(name, condition):
        if not condition:
            raise RuntimeError(name + "_failed")
        checks.append({"name": name, "status": "passed"})
        print(json.dumps(checks[-1]), flush=True)

    def wait_ready():
        for _ in range(120):
            try:
                if request("/product/info", credential="")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError("product_not_ready")

    try:
        command("compose-config", ["config", "--quiet"])
        command("image-build", ["build", "--no-cache", "--pull"])
        command("compose-start", ["up", "-d", "--wait", "--wait-timeout", "240"])
        wait_ready()
        _, info = request("/product/info", credential="")
        ok(
            "explicit_synthetic_generation_and_quality_boundary",
            info["is_simulated_generation"]
            and info["model_quality_acceptance"] == "not_established",
        )
        ok(
            "unauthorized_catalog_denied",
            request("/product/catalog", credential="invalid")[0] == 403,
        )
        status, catalog = request("/product/catalog")
        ok("authenticated_catalog", status == 200 and bool(catalog["releases"]))
        release = catalog["releases"][0]
        task_id = "CONTAINER-" + secrets.token_hex(4).upper()
        source = next(s for s in release["source_ids"] if "10V039000" in s)
        plan = {
            "release_id": release["release_id"],
            "allowed_source_ids": [source],
            "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            "request": {
                "task_id": task_id,
                "question": "SYNTHETIC container investigation",
                "retrieval_query": "brake",
                "intent": "record_lookup",
            },
            "mode": "PLAN",
        }
        status, task = request("/tasks", {"task_id": task_id, "plan": plan})
        ok("task_created_from_clean_synthetic_release", status == 200 and task["state"] == "DRAFT")
        status, research = request(
            "/tasks/" + task_id + "/research", {"operation": "search", "query": "brake"}
        )
        ok(
            "real_frozen_source_research",
            status == 200 and bool(research["result"]["matched_hits"]),
        )
        _, task = request("/tasks/" + task_id)
        plan = task["plan"] | {"mode": "EXECUTE"}
        _, task = request(
            "/tasks/" + task_id + "/revisions", {"expected_version": task["version"], "plan": plan}
        )
        question = next(q for q in task["questions"] if q["kind"] == "approval")
        _, task = request(
            "/tasks/" + task_id + "/approvals",
            {
                "expected_version": task["version"],
                "question_id": question["question_id"],
                "revision_id": task["revision_id"],
                "plan_sha256": task["plan_sha256"],
                "approve": True,
            },
        )
        status, _ = request("/tasks/" + task_id + "/execute", {"expected_version": task["version"]})
        ok("version_bound_execution_enqueued", status == 200)
        for _ in range(120):
            _, task = request("/tasks/" + task_id)
            if task["state"] in {"COMPLETED", "FAILED", "PAUSED"}:
                break
            time.sleep(1)
        ok("durable_worker_completed", task["state"] == "COMPLETED")
        _, report = request("/tasks/" + task_id + "/report")
        ok(
            "completed_does_not_mask_evidence_insufficiency",
            report["proposal"]["disposition"] == "insufficient_evidence",
        )
        alias = next(
            a
            for a, identifier in report["aliases"].items()
            if any(c["evidence_id"] == identifier and c["kind"] == "text" for c in report["cards"])
        )
        status, _ = request("/product/tasks/" + task_id + "/original/" + alias)
        ok("original_source_replay", status == 200)
        result["task_id"] = task_id
        result["report_sha256"] = hashlib.sha256(
            json.dumps(report, sort_keys=True).encode()
        ).hexdigest()
        command("product-restart", ["restart", "product"])
        wait_ready()
        status, persisted = request("/tasks/" + task_id)
        ok(
            "task_and_identity_survive_product_restart",
            status == 200 and persisted["state"] == "COMPLETED",
        )
        session = root / "session.json"
        session.write_text(
            json.dumps({"url": base, "token": token, "profile": "synthetic-demo"}), encoding="utf-8"
        )
        session.chmod(0o600)
        result["success"] = True
    except Exception as error:
        result["error_type"] = type(error).__name__
        # Error messages are deliberately bounded; never export HTTP bodies or environment.
        if isinstance(error, RuntimeError):
            result["error_code"] = str(error)
        raise
    finally:
        if not (args.keep_running and result["success"]):
            try:
                command("compose-stop", ["down"], timeout=120)
                result["services_stopped"] = True
            except Exception:
                result["cleanup_failed"] = True
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"success": True, "checks": len(checks), "services_stopped": result["services_stopped"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

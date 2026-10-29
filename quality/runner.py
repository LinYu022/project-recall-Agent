"""A bounded new reviewer ledger; previous generation authorizations are never reused."""

import json
from datetime import UTC, datetime
from pathlib import Path

from ingestion.records import content_hash
from models.chat_api import ChatJSONModel, ModelCallFailed, local_configuration
from quality.preparation import implementation
from quality.scoring import metrics
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore


def check_approval(plan_id, jobs, approval):
    if (
        approval.get("plan_id") != plan_id
        or approval.get("source_kind") != "actual_human_reply"
        or approval.get("status") != "explicit_user_approved"
        or not approval.get("human_reply")
        or set(approval.get("jobs", [])) != {j["job_id"] for j in jobs}
        or approval.get("model") != "deepseek-v4-pro"
        or approval.get("endpoint") != "https://api.deepseek.com"
        or approval.get("maximum_requests") != 2 * len(jobs)
    ):
        raise ValueError("no_matching_new_reviewer_batch_authorization")


def run(pointer, approval_path, output, *, calibration_only=True, repair_invalid=False):
    from evaluation.records import atomic_json, exclusive

    plan = read(pointer["path"])
    if content_hash(plan) != pointer["plan_id"] or plan["implementation"] != implementation():
        raise ValueError("quality_plan_or_code_changed_requires_new_identity")
    jobs = (
        [j for j in plan["jobs"] if j["cohort"].startswith("calibration")]
        if calibration_only
        else plan["jobs"]
    )
    approval = read(approval_path)
    check_approval(pointer["plan_id"], jobs, approval)
    output.mkdir(parents=True, exist_ok=True)
    ledger_path = output / "requests.json"
    store, inputs = ArtifactStore(output / "provider"), ArtifactStore(Path(plan["input_store"]))
    config = local_configuration() | {"RECALL_MODEL_ID": plan["model"]}
    model = ChatJSONModel(config)
    spec = {
        "plan_id": pointer["plan_id"],
        "jobs": [j["job_id"] for j in jobs],
        "maximum_requests": 2 * len(jobs),
        "per_job_requests": 2,
        "per_request_completion_tokens": 8192,
        "approval_sha256": content_hash(approval),
    }
    with exclusive(ledger_path):
        if not ledger_path.exists():
            atomic_json(ledger_path, {"spec": spec, "entries": []})
        elif read(ledger_path)["spec"] != spec:
            raise ValueError("review_allowance_changed")
    for job in jobs:
        result_path = output / "results" / (job["job_id"] + ".json")
        prior = read(result_path) if result_path.exists() else None
        if prior:
            if not repair_invalid or prior["metrics"] is not None:
                continue
            error_code = prior["error"] or ""
            if not error_code.startswith(
                (
                    "review_schema_or_coverage_invalid",
                    "review_completion_truncated",
                    "model_content_invalid_json",
                )
            ):
                continue
            result_path = output / "results" / (job["job_id"] + "-repair1.json")
            if result_path.exists():
                continue
        payload = inputs.read_json(job["input"]["sha256"])
        data = read(ledger_path)
        attempts = [e for e in data["entries"] if e["job_id"] == job["job_id"]]
        if any(e["state"] == "reserved" for e in attempts):
            raise ValueError("unknown_prior_request_no_automatic_resend")
        # One run makes at most one request per job. A second schema-repair attempt is explicit.
        if attempts and not (repair_invalid and len(attempts) == 1 and prior):
            continue
        if prior:
            previous = store.read_json(prior["provider_artifact"]["sha256"])
            payload = payload | {
                "schema_repair_only": {
                    "mechanical_error": prior["error"],
                    "previous_response": (previous["response"] or {}).get("raw_content"),
                    "instruction": "只修正机械格式/覆盖错误，重新核查全部原始证据；不可参考人类分数、重复运行至质量通过。",
                }
            }
        if (
            len(plan["system"] + json.dumps(payload, ensure_ascii=False))
            > plan["per_input_character_cap"]
        ):
            raise ValueError("review_repair_input_exceeds_declared_character_cap")
        request_ref = store.put_json(
            {"system": plan["system"], "payload": payload, "max_tokens": 8192}
        )
        with exclusive(ledger_path):
            data = read(ledger_path)
            if len(data["entries"]) >= spec["maximum_requests"]:
                raise ValueError("review_budget_exhausted")
            ticket = len(data["entries"])
            data["entries"].append(
                {
                    "ticket": ticket,
                    "job_id": job["job_id"],
                    "state": "reserved",
                    "input_sha256": job["input"]["sha256"],
                    "request_artifact": request_ref,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            atomic_json(ledger_path, data)
        response, scored, error = None, None, None
        try:
            response = model.generate(plan["system"], payload, max_tokens=8192)
            if not response["accepted_complete"]:
                raise ModelCallFailed("review_completion_truncated")
            scored = metrics(payload, response["proposal"])
        except ModelCallFailed as exc:
            error = exc.code
            if response is None:
                response = exc.diagnostics
        except ValueError as exc:
            error = "review_schema_or_coverage_invalid:" + str(exc)[:300]
        ref = store.put_json(
            {
                "job_id": job["job_id"],
                "input_sha256": job["input"]["sha256"],
                "response": response,
                "metrics": scored,
                "error": error,
            }
        )
        with exclusive(ledger_path):
            data = read(ledger_path)
            data["entries"][ticket].update(
                state="completed" if scored else "failed",
                artifact=ref,
                usage=(response or {}).get("usage"),
                error=error,
            )
            atomic_json(ledger_path, data)
        write_new(
            result_path,
            {
                "job_id": job["job_id"],
                "provider_artifact": ref,
                "metrics": scored,
                "error": error,
                "reviewer_kind": "model",
            },
        )
        print(
            json.dumps(
                {
                    "job_id": job["job_id"],
                    "status": "scored" if scored else "invalid",
                    "error": error,
                }
            ),
            flush=True,
        )

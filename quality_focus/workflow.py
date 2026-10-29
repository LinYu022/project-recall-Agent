"""Three bounded calibration inputs with exact source-quote checks."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ingestion.records import content_hash
from models.chat_api import ChatJSONModel, ModelCallFailed, local_configuration
from quality_focus.prompt import SYSTEM
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore


def implementation():
    return {
        p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path("quality_focus").glob("*.py"))
    }


def prepare(root):
    pointer = read(".local/q16/calibration-final/active-review-plan.json")
    old = read(pointer["path"])
    inputs = ArtifactStore(Path(old["input_store"]))
    byid = {j["job_id"]: j for j in old["jobs"]}
    store = ArtifactStore(root / "inputs")
    jobs = []
    for identifier, key, claims in [
        ("FQC-QUALIFIER", "DEV-BLIND-95cee9ebfdee", ["C002"]),
        ("FQC-BOUNDARY", "OLD-BLIND-525c27d5cddc", ["C002", "C006"]),
    ]:
        packet = inputs.read_json(byid[key]["input"]["sha256"])
        payload = {
            "mode": "claims",
            "question": packet["question"],
            "scope": packet["scope"],
            "selected_claims": [c for c in packet["proposal"]["claims"] if c["claim_id"] in claims],
            "evidence_groups": packet["evidence_groups"],
            "reference_sources": packet["reference_sources"],
            "program_context_pairs": packet["program_context_pairs"],
        }
        jobs.append({"job_id": identifier, "input": store.put_json(payload), "source_job": key})
    cases = []
    for key, stage in [
        ("DEV-BLIND-53ac2882b280", "clarify_missing_model_year"),
        (
            "DEV-BLIND-3bbc60801d77",
            "clarify_individual_conditions_without_confirming_applicability",
        ),
        ("DEV-BLIND-5f63e8d13912", "bounded_insufficient_complaint_data"),
        ("DEV-BLIND-84378628fdad", "wait_for_explicit_time_scope_acceptance"),
    ]:
        packet = inputs.read_json(byid[key]["input"]["sha256"])
        cases.append(packet | {"required_stage": stage})
    jobs.append(
        {"job_id": "FQC-STAGES", "input": store.put_json({"mode": "stages", "cases": cases})}
    )
    plan = {
        "schema_version": "focused-quality-calibration/0.2",
        "model": "deepseek-v4-pro",
        "endpoint": "https://api.deepseek.com",
        "system": SYSTEM,
        "jobs": jobs,
        "input_store": (root / "inputs").as_posix(),
        "implementation": implementation(),
        "previous_plan_id": pointer["plan_id"],
        "previous_results": "docs/verification/2026-10-29-P1-16-quality-calibration-v1.json",
        "maximum_requests": 6,
        "per_job_requests": 2,
        "max_tokens": 8192,
        "character_cap": 160000,
        "human_grades_sent": False,
        "purpose": "scorer_development_not_confirmatory_generation",
    }
    identity = content_hash(plan)
    write_new(root / (identity + ".json"), plan)
    p = {"plan_id": identity, "path": (root / (identity + ".json")).as_posix()}
    write_new(root / "plan.json", p)
    return p, plan


def validate(payload, value):
    if value.get("mode") != payload["mode"]:
        raise ValueError("focus_mode_changed")
    if payload["mode"] == "stages":
        expected = {c["blind_id"] for c in payload["cases"]}
        if {c["blind_id"] for c in value["cases"]} != expected or len(value["cases"]) != len(
            expected
        ):
            raise ValueError("incomplete_stage_denominator")
        for c in value["cases"]:
            if any(
                c[key] is not None and type(c[key]) is not bool
                for key in ("stage_appropriate", "stage_requirements_satisfied")
            ):
                raise ValueError("stage_verdict_invalid")
            if c["full_individual_or_historical_investigation_complete"] is not False:
                raise ValueError("stage_invents_complete_individual_investigation")
        return value
    claims = {c["claim_id"]: c for c in payload["selected_claims"]}
    if {c["claim_id"] for c in value["claims"]} != set(claims) or len(value["claims"]) != len(
        claims
    ):
        raise ValueError("incomplete_focused_claims")
    aliases = {a: e for e in payload["evidence_groups"] for a in e["aliases"]}
    for c in value["claims"]:
        if c["support_verdict"] not in {"supported", "partial", "unsupported", "not_evaluable"}:
            raise ValueError("focused_support_verdict_invalid")
        if c["scope_verdict"] not in {
            "correct",
            "minor_caveat",
            "partial",
            "incorrect",
            "not_evaluable",
        }:
            raise ValueError("focused_scope_verdict_invalid")
        if c["qualifier_preserved"] is not None and type(c["qualifier_preserved"]) is not bool:
            raise ValueError("focused_qualifier_verdict_invalid")
        original = claims[c["claim_id"]]
        cursor = 0
        covered: set[int] = set()
        for unit in c["units"]:
            if not unit["quote"] or unit["verdict"] not in {
                "supported",
                "partial",
                "unsupported",
                "not_evaluable",
            }:
                raise ValueError("focused_unit_invalid")
            start = original["statement"].find(unit["quote"], cursor)
            if start < 0:
                raise ValueError("focus_unit_not_exact_nonoverlapping")
            end = start + len(unit["quote"])
            covered.update(range(start, end))
            cursor = end
            if unit["evidence_alias"] is not None:
                if unit["evidence_alias"] not in original["support_ids"]:
                    raise ValueError("focus_adds_uncited_evidence")
                if unit["source_quote"] is not None and unit["source_quote"] not in json.dumps(
                    aliases[unit["evidence_alias"]]["payload"], ensure_ascii=False
                ):
                    raise ValueError("focus_source_quote_not_exact")
        if any(ch.isalnum() and i not in covered for i, ch in enumerate(original["statement"])):
            raise ValueError("focus_units_omit_meaningful_text")
    return value


def run(pointer, approval_path, root, *, repair=False):
    from evaluation.records import atomic_json, exclusive

    plan = read(pointer["path"])
    if content_hash(plan) != pointer["plan_id"] or implementation() != plan["implementation"]:
        raise ValueError("focused_calibration_identity_changed")
    approval = read(approval_path)
    if (
        approval.get("plan_id") != pointer["plan_id"]
        or approval.get("maximum_requests") != 6
        or approval.get("source_kind") != "actual_human_reply"
        or not approval.get("human_reply")
        or approval.get("status") != "explicit_user_approved"
    ):
        raise ValueError("no_new_focused_calibration_approval")
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "requests.json"
    if not ledger.exists():
        write_new(ledger, {"plan_id": pointer["plan_id"], "maximum_requests": 6, "entries": []})
    store = ArtifactStore(root / "provider")
    inputs = ArtifactStore(Path(plan["input_store"]))
    model = ChatJSONModel(local_configuration() | {"RECALL_MODEL_ID": plan["model"]})
    for job in plan["jobs"]:
        result = root / (job["job_id"] + ".json")
        prior = read(result) if result.exists() else None
        if prior and (not repair or prior["error"] is None):
            continue
        if prior:
            result = root / (job["job_id"] + "-repair1.json")
        if result.exists():
            continue
        old = read(ledger)
        entries = [e for e in old["entries"] if e["job_id"] == job["job_id"]]
        if any(e["state"] == "reserved" for e in entries):
            raise ValueError("unknown_focus_request_no_retry")
        if len(entries) >= 2 or (entries and not prior):
            continue
        payload = inputs.read_json(job["input"]["sha256"])
        if prior:
            prev = store.read_json(prior["artifact"]["sha256"])
            if prior["error"] == "model_transport_failed":
                continue
            payload = payload | {
                "mechanical_repair": {
                    "error": prior["error"],
                    "previous_output": (prev["response"] or {}).get("raw_content"),
                }
            }
        if len(SYSTEM + json.dumps(payload, ensure_ascii=False)) > 160000:
            raise ValueError("focus_input_budget_exceeded")
        request_ref = store.put_json({"system": SYSTEM, "payload": payload, "max_tokens": 8192})
        with exclusive(ledger):
            old = read(ledger)
            if len(old["entries"]) >= 6:
                raise ValueError("focus_budget_exhausted")
            ticket = len(old["entries"])
            old["entries"].append(
                {
                    "job_id": job["job_id"],
                    "state": "reserved",
                    "input_artifact": request_ref,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            atomic_json(ledger, old)
        response = judgment = error = None
        try:
            response = model.generate(SYSTEM, payload, max_tokens=8192)
            if not response["accepted_complete"]:
                raise ModelCallFailed("focus_truncated")
            judgment = validate(inputs.read_json(job["input"]["sha256"]), response["proposal"])
        except ModelCallFailed as e:
            error = e.code
            if response is None:
                response = e.diagnostics
        except (ValueError, KeyError, TypeError) as e:
            error = "focus_schema_invalid:" + str(e)[:250]
        ref = store.put_json(
            {"response": response, "judgment": judgment, "error": error, "job_id": job["job_id"]}
        )
        with exclusive(ledger):
            old = read(ledger)
            old["entries"][ticket].update(
                state="completed" if judgment else "failed",
                artifact=ref,
                usage=(response or {}).get("usage"),
            )
            atomic_json(ledger, old)
        write_new(
            result, {"job_id": job["job_id"], "artifact": ref, "judgment": judgment, "error": error}
        )
        print(
            {"job_id": job["job_id"], "status": "valid" if judgment else "invalid", "error": error},
            flush=True,
        )

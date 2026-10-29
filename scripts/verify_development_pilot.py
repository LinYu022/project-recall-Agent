"""Read-only proof of development preparation, never a model-quality score."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from baselining.preparation import checked
from ingestion.records import content_hash
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore


def inspect_input(value, allowed):
    forbidden = {
        "required_claims",
        "evidence_sets",
        "reference_sha256",
        "reference_claim_id",
        "acceptable_dispositions",
        "reviewer_id",
        "reviewer_kind",
        "api_key",
        "deepseek_api_key",
        "recall_model_api_key",
        "bearer_token",
        "secret",
    }
    used = set()

    def walk(item):
        if isinstance(item, dict):
            if {str(k).lower() for k in item} & forbidden:
                raise ValueError("review_label_or_credential_field_in_preview_input")
            for key in ("source_ids", "allowed_source_ids"):
                if key in item:
                    used.update(item[key])
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value["payload"])
    text = json.dumps(value, ensure_ascii=False)
    if not used <= allowed or "NHTSA-EVAL-CAMPAIGN-" in text:
        raise ValueError("preview_input_contains_source_outside_development_task")
    if re.search(r"\bsk-[A-Za-z0-9_-]{20,}", text):
        raise ValueError("credential_like_value_in_preview_input")
    return {
        "source_ids": sorted(used),
        "reference_or_credential_field_found": False,
        "input_content_sha256": content_hash(value),
        "serialized_system_and_payload_characters": len(value["system"])
        + len(json.dumps(value["payload"], ensure_ascii=False)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--offline-record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pointer = read(args.plan)
    plan, _, cells = checked(pointer)
    assert plan == read("evals/development-baseline-v0.1/prepared-plan.json")
    preview = read(args.preview)
    assert preview["plan_id"] == pointer["plan_id"] and preview["mode"] == "preview"
    assert preview["denominator"] == 8 and preview["new_provider_requests"] == 0
    assert preview["server_stopped"] is True
    assert all(r.get("preview_expectation_met") is True for r in preview["rows"])
    assert {r["cell_id"] for r in preview["rows"]} == {c["cell_id"] for c in cells}
    root = Path(preview["root"])
    store = ArtifactStore(root)
    inputs = ArtifactStore(root / "provider")
    observations = read(root / "runs.json")
    input_checks, observed_states = [], []
    for cell in cells:
        case = next(c for c in plan["cases"] if c["case_id"] == cell["case_id"])
        row = observations["rows"][cell["cell_id"]]
        obs = store.read_json(row["record"]["sha256"])
        for ref in obs["preview_inputs"]:
            value = inputs.read_json(ref["sha256"])
            input_checks.append(
                {
                    "cell_id": cell["cell_id"],
                    "artifact_sha256": ref["sha256"],
                    **inspect_input(value, set(case["sources"])),
                }
            )
        observed_states.append(
            {
                "cell_id": cell["cell_id"],
                "state": obs["state"],
                "task_state": obs.get("task_state"),
                "end_to_end_ms": obs["end_to_end_ms"],
                "guard": obs.get("guard"),
                "error_code": obs.get("error_code"),
            }
        )
    assert len(input_checks) == 3
    frozen = read("docs/verification/2026-10-28-P1-16-evaluation.json")
    old = read(".local/P1-16/provider-request-allowance.json")
    legacy = read(".local/P1-09/request-allowance.json")
    assert content_hash(old) == frozen["raw_records"]["ledger"] and len(old["entries"]) == 209
    assert content_hash(legacy) == frozen["raw_records"]["legacy_ledger"]
    assert len(legacy["entries"]) == 12
    # Approval and a new provider ledger must not be created as a side effect of a preview.
    assert not Path(".local/P1-16/development-baseline-provider-allowance.json").exists()
    offline = read(args.offline_record)
    assert offline["tests"] == {"tests": 481, "failures": 0, "errors": 0, "skipped": 0}
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    write_new(
        args.output,
        {
            "schema_version": "P1-16-development-preparation/0.1",
            "classification": "actual_local_preflight_and_engineering_verification_not_a_provider_quality_baseline",
            "plan_id": pointer["plan_id"],
            "preview_content_sha256": content_hash(preview),
            "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "design_content_sha256": content_hash(plan["design"]),
            "maximum_proposed_requests": 3,
            "observed_new_provider_requests": 0,
            "preview": preview,
            "observed_states": observed_states,
            "inputs": input_checks,
            "offline_verification": offline,
            "frozen_generation_unchanged": True,
            "P1_16_requests_unchanged": 209,
            "P1_16_ledger_content_sha256": content_hash(old),
            "P1_09_requests_unchanged": 12,
            "P1_09_ledger_content_sha256": content_hash(legacy),
            "actual_independent_human_reviews_for_this_pilot": 0,
            "reference_author": "implementation_agent_pending_independent_review",
            "quality_baseline": None,
            "numeric_quality_target": None,
            "P1_17_started": False,
        },
    )
    print(json.dumps({"status": "verified_preparation_only", "output": args.output.as_posix()}))


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the separately approved pilot from saved inputs/responses, without new requests."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from baselining.preparation import checked
from baselining.runner import LEDGER, check_approval
from ingestion.records import content_hash
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore
from scripts.verify_development_pilot import inspect_input


def reviewed_baseline(reviews, cells):
    expected = {c["case_id"] for c in cells}
    observed = {r["case_id"] for r in reviews}
    if len(observed) != len(reviews):
        raise ValueError("duplicate_case_reviews_require_actual_adjudication")
    if not observed <= expected:
        raise ValueError("review_outside_development_denominator")
    if observed != expected:
        return None
    answers = [r for r in reviews if r["review_scope"] == "generated_answer"]
    stops = [r for r in reviews if r["review_scope"] == "stop_or_limited_answer"]
    return {
        "classification": "fully_reviewed_bounded_development_pilot_descriptive_only",
        "reviewed_tasks": len(reviews),
        "reviewer_count": len({r["reviewer_id"] for r in reviews}),
        "event_clusters": len({c["event_cluster_id"] for c in cells}),
        "generated_answer_tasks": len(answers),
        "generated_core_complete": sum(r["core_task_complete"] is True for r in answers),
        "stop_or_limited_answer_tasks": len(stops),
        "appropriate_stage_handling": sum(
            r["stop_or_limited_answer_requirements_satisfied"] is True for r in stops
        ),
        "dispositions_appropriate": sum(r["disposition_correct"] is True for r in reviews),
        "requirements_verdict_counts": dict(
            Counter(r["all_required_conditions_verdict"] for r in reviews)
        ),
        "strict_atomic_metrics": None,
        "population_quality": None,
        "confidence_interval": None,
        "limits": "one human; two seen event clusters; one method; no independent test or method ranking",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pointer = read(args.plan)
    plan, _, cells = checked(pointer)
    approval = read("docs/experiments/P1-16-development-pilot-approval.json")
    check_approval(pointer, plan, approval)
    summary = read(args.summary)
    assert summary["plan_id"] == pointer["plan_id"] and summary["mode"] == "live"
    assert summary["server_stopped"] is True and summary["denominator"] == len(cells)
    ledger = read(LEDGER)
    assert ledger["spec"]["experiment_id"] == pointer["plan_id"]
    assert ledger["spec"]["maximum_requests"] == 3
    assert len(ledger["entries"]) <= 3 <= approval["human_authorized_batch_maximum_requests"]
    root = Path(summary["root"])
    store, provider = ArtifactStore(root), ArtifactStore(root / "provider")
    index = read(root / "runs.json")
    requests = []
    for entry in ledger["entries"]:
        if entry.get("state") == "reserved":
            raise ValueError("live_pilot_has_unknown_inflight_request_keep_missingness")
        response = provider.read_json(entry["artifact"]["sha256"])
        ref = response["input_artifact"]
        value = provider.read_json(ref["sha256"])
        assert content_hash(value) == entry["fingerprint"]
        cell = next(c for c in cells if c["cell_id"] == entry["cell"])
        case = next(c for c in plan["cases"] if c["case_id"] == cell["case_id"])
        assert value["identity"]["model_id"] == plan["design"]["model"]
        assert value["identity"]["is_simulated"] is False
        assert entry["usage"] == (response.get("result") or response.get("diagnostics") or {}).get(
            "usage"
        )
        usage = entry.get("usage") or {}
        if all(
            type(usage.get(k)) is int
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            assert usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"]
        if all(
            type(usage.get(k)) is int
            for k in ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        ):
            assert (
                usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
                == usage["prompt_tokens"]
            )
        assert 0 < value["max_tokens"] <= plan["profile"]["generation_tokens"]
        requests.append(
            {
                "cell_id": entry["cell"],
                "state": entry["state"],
                "response_artifact_sha256": entry["artifact"]["sha256"],
                "input_artifact_sha256": ref["sha256"],
                "usage": entry["usage"],
                "error_code": response.get("error_code"),
                **inspect_input(value, set(case["sources"])),
            }
        )
    totals = {}
    for field in [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ]:
        known = [
            r["usage"][field]
            for r in requests
            if isinstance(r["usage"], dict) and type(r["usage"].get(field)) is int
        ]
        totals[field] = {
            "known_subtotal": sum(known),
            "missing_requests": len(requests) - len(known),
            "total": sum(known) if len(known) == len(requests) else None,
        }
    outputs = []
    for cell in cells:
        row = index["rows"][cell["cell_id"]]
        if not row["record"]:
            raise ValueError("live_pilot_cell_unobserved_keep_original_state")
        obs = store.read_json(row["record"]["sha256"])
        assert obs["cell"] == cell and obs["state"] == row["state"]
        count = sum(r["cell_id"] == cell["cell_id"] for r in requests)
        assert count <= ledger["spec"]["cells"][cell["cell_id"]]
        result = {
            "cell": cell,
            "state": row["state"],
            "requests": count,
            "observation_sha256": content_hash(obs),
            "task_state": obs.get("task_state"),
            "end_to_end_ms": obs["end_to_end_ms"],
            "guard": obs.get("guard"),
            "error_code": obs.get("error_code"),
            "formal_semantic_metrics": None,
        }
        if obs.get("report_reference"):
            ref = obs["report_reference"]
            report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                ref["report_artifact"]["sha256"]
            )
            assert content_hash(report) == obs["report_content_sha256"]
            result |= {
                "report_content_sha256": content_hash(report),
                "disposition": (report.get("proposal") or {}).get("disposition"),
                "claims": (report.get("proposal") or {}).get("claims"),
                "evidence_verification": report.get("evidence_verification"),
                "claim_verification": report.get("claim_verification"),
            }
        packet = read(root / "review-packets" / (obs["blind_id"] + ".json"))
        result |= {
            "review_packet_path": (
                root / "review-packets" / (obs["blind_id"] + ".json")
            ).as_posix(),
            "review_packet_content_sha256": content_hash(packet),
            "blind_id": obs["blind_id"],
        }
        outputs.append(result)
    frozen = read("docs/verification/2026-10-28-P1-16-evaluation.json")
    old = read(".local/P1-16/provider-request-allowance.json")
    legacy = read(".local/P1-09/request-allowance.json")
    assert content_hash(old) == frozen["raw_records"]["ledger"]
    assert content_hash(legacy) == frozen["raw_records"]["legacy_ledger"]
    human_reviews = []
    for path in sorted(Path("docs/experiments").glob("P1-16-development-human-review-DEV2-*.json")):
        review = read(path)
        if review["plan_id"] != pointer["plan_id"]:
            continue
        row = next(r for r in outputs if r["cell"]["cell_id"] == review["cell_id"])
        assert review["reviewer_kind"] == "human" and review["source_kind"] == "actual_human_reply"
        assert review["source_question_id"]
        assert review["observation_sha256"] == row["observation_sha256"]
        assert review["report_content_sha256"] == row.get("report_content_sha256")
        assert review["packet_content_sha256"] == row["review_packet_content_sha256"]
        case = next(c for c in plan["cases"] if c["case_id"] == review["case_id"])
        assert review["reference_sha256"] == case["reference_sha256"]
        assert set(review["claim_verdicts"]) == {c["claim_id"] for c in row.get("claims") or []}
        packet_path = Path(row["review_packet_path"])
        md = Path(review.get("packet_markdown_path", packet_path.with_suffix(".md")))
        assert md.resolve().parent == packet_path.resolve().parent
        case_design = next(c for c in plan["design"]["cases"] if c["case_id"] == review["case_id"])
        if review.get("review_bundle_path"):
            assert case_design["maximum_requests"] == 0
            bundle_path = Path(review["review_bundle_path"])
            assert bundle_path.resolve() == (packet_path.parent / "stops-review.json").resolve()
            bundle = read(bundle_path)
            assert content_hash(bundle) == review["review_bundle_content_sha256"]
            assert bundle["plan_id"] == pointer["plan_id"]
            item = next(i for i in bundle["items"] if i["case_id"] == review["case_id"])
            assert item["observation_sha256"] == row["observation_sha256"]
            assert content_hash(item["packet"]) == row["review_packet_content_sha256"]
            assert md.name == "stops-review.md"
            review_scope = "stop_or_limited_answer"
            assert review["core_task_complete"] is None
        else:
            assert case_design["maximum_requests"] == 1
            assert md.name in {
                review["blind_id"] + suffix for suffix in (".md", "-reading.md", "-reading-v2.md")
            }
            review_scope = "generated_answer"
        assert hashlib.sha256(md.read_bytes()).hexdigest() == review["packet_markdown_sha256"]
        binary = review["all_required_conditions_satisfied"]
        assert binary is None or type(binary) is bool
        requirement_verdict = review.get("all_required_conditions_verdict")
        if requirement_verdict is None:
            requirement_verdict = (
                "satisfied"
                if binary is True
                else "not_satisfied"
                if binary is False
                else "not_evaluated"
            )
        # A qualified positive human judgment cannot be silently collapsed to True or False.
        if requirement_verdict == "mostly_satisfied_with_minor_caveat":
            assert binary is None and review["all_required_conditions_reviewed"] is True
        human_reviews.append(
            {
                "path": path.as_posix(),
                "content_sha256": content_hash(review),
                "case_id": review["case_id"],
                "reviewer_id": review["reviewer_id"],
                "qualitative_task_success": review["task_success"],
                "qualitative_task_success_scope": review["task_success_scope"],
                "core_task_complete": review["core_task_complete"],
                "review_scope": review_scope,
                "stop_or_limited_answer_requirements_satisfied": review.get(
                    "stop_or_limited_answer_requirements_satisfied"
                ),
                "disposition_correct": review["disposition_correct"],
                "all_required_conditions_satisfied": review["all_required_conditions_satisfied"],
                "all_required_conditions_verdict": requirement_verdict,
                "all_required_conditions_quote": review["all_required_conditions_quote"],
                "reviewed_claims": len(review["claim_verdicts"]),
                "atomic_metrics": review["formal_uniform_atomic_metrics"],
            }
        )
    write_new(
        args.output,
        {
            "schema_version": "P1-16-development-live-audit/0.3",
            "plan_id": pointer["plan_id"],
            "classification": "actual_provider_development_pilot_pending_independent_review",
            "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "approved_human_maximum_requests": approval["human_authorized_batch_maximum_requests"],
            "enforced_plan_maximum_requests": 3,
            "actual_new_provider_requests": len(requests),
            "provider_states": dict(Counter(r["state"] for r in requests)),
            "provider_ledger_content_sha256": content_hash(ledger),
            "usage": totals,
            "requests": requests,
            "denominator": len(cells),
            "rows": outputs,
            "states": dict(Counter(r["state"] for r in outputs)),
            "actual_monetary_cost": None,
            "server_stopped": summary["server_stopped"],
            "original_frozen_batch_requests": len(old["entries"]),
            "original_frozen_ledger_content_sha256": content_hash(old),
            "legacy_P1_09_requests": len(legacy["entries"]),
            "legacy_ledger_content_sha256": content_hash(legacy),
            "independent_human_reviews_for_this_pilot": len(human_reviews),
            "actual_human_review_records": human_reviews,
            "human_reviewed_cells": len({r["case_id"] for r in human_reviews}),
            "human_unreviewed_cells": len(cells) - len({r["case_id"] for r in human_reviews}),
            "strict_task_completion_rate": None,
            "quality_baseline": reviewed_baseline(human_reviews, cells),
            "numeric_quality_target": None,
            "P1_17_started": False,
        },
    )
    print(
        json.dumps(
            {
                "status": "audited_saved_live_results",
                "new_requests": len(requests),
                "output": args.output.as_posix(),
            }
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""Truthful public experiment summary: retain failures, unknown usage and replay outcomes."""

import json
from pathlib import Path

from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def run_summary(root, run):
    """Resolve immutable report bytes; reports/ is a replaceable convenience view."""
    digest = run["report_artifact"]["sha256"]
    matches = list((root / "runs/private-runs").rglob(digest + ".json"))
    assert len(matches) == 1
    report = ArtifactStore(matches[0].parent.parent).read_json(digest)
    assert content_hash(report) == run["report_content_sha256"]
    return {
        k: run.get(k)
        for k in (
            "case_id",
            "method",
            "task_state",
            "execution_status",
            "disposition",
            "generation_mode",
            "model_calls",
            "tool_calls",
            "termination",
            "error_code",
            "resources",
            "elapsed_ms",
            "report_content_sha256",
            "report_artifact",
            "score",
        )
    } | {
        "effective_config": report["config"],
        "prompt_version": report["prompt_version"],
        "implementation_sha256": report["implementation_sha256"],
        "model_identity": report["model_identity"],
        "release_id": report["release_id"],
    }


def main():
    root = Path(".local/P1-09")
    offline = json.loads((root / "offline-delivery.json").read_bytes())
    replay_record = json.loads((root / "replay-final.json").read_bytes())
    replay = replay_record["single_agent"]
    ledger = json.loads((root / "request-allowance.json").read_bytes())
    assert len(ledger["entries"]) <= 14
    assert all(e["status"] != "reserved" for e in ledger["entries"])
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert replay_record["tests"]["failures"] == replay_record["tests"]["errors"] == 0
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert replay_record["agent_check_exit_code"] == 0
    assert replay["external_model_calls"] == 0 and replay["replay_outcomes_match"]
    assert (
        replay_record["server_stopped"]
        and replay["http_server_stopped"]
        and replay["schema_removed"]
    )
    rounds = []
    for label, name in [
        ("initial_same_envelope", "live-round1/live-comparison.json"),
        ("decision_protocol_repair", "live-round2/live-revised.json"),
        ("candidate_precondition_repair", "live-final.json"),
    ]:
        record = json.loads((root / name).read_bytes())
        experiment = record["single_agent"]
        rounds.append(
            {
                "round": label,
                "record_content_sha256": content_hash(record),
                "new_requests": experiment["external_model_calls"],
                "replays": experiment["recorded_response_replays"],
                "profile": experiment["profile"],
                "runs": [run_summary(root, r) for r in experiment["runs"]],
            }
        )
    assert sum(r["new_requests"] for r in rounds) == len(ledger["entries"])
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    known = {
        k: sum(
            e["usage"][k]
            for e in ledger["entries"]
            if isinstance(e.get("usage"), dict) and type(e["usage"].get(k)) is int
        )
        for k in keys
    }
    unknown = [
        e["sequence"]
        for e in ledger["entries"]
        if not isinstance(e.get("usage"), dict)
        or any(type(e["usage"].get(k)) is not int for k in keys)
    ]
    value = {
        "schema_version": "P1-09-live-results/0.1",
        "timestamp": replay_record["timestamp"],
        "classification": "previously_seen_development_cases_real_api_failures_and_bounded_repair_rounds",
        "approved_limit": 14,
        "actual_new_requests": len(ledger["entries"]),
        "unused_requests": 14 - len(ledger["entries"]),
        "per_case_request_counts": {
            case: {
                method: sum(
                    e["case_id"] == case and e["method"] == method for e in ledger["entries"]
                )
                for method in ("fixed", "single_agent")
            }
            for case in ledger["case_limits"]
        },
        "known_provider_usage_subset": known,
        "unknown_usage_request_sequences": unknown,
        "total_real_usage": None if unknown else known,
        "estimated_cost": None,
        "rounds": rounds,
        "verification": {
            "record_content_sha256": {
                "offline": content_hash(offline),
                "replay": content_hash(replay_record),
                "request_ledger": content_hash(ledger),
            },
            "offline_tests": offline["tests"],
            "postgres_tests": replay_record["tests"],
            "offline_exit_codes": [c["exit_code"] for c in offline["checks"]],
            "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
            "exact_final_outcomes_replayed": replay["replay_outcomes_match"],
            "replay_new_requests": replay["external_model_calls"],
            "replayed_model_responses": replay["recorded_response_replays"],
            "postgres_stopped": replay_record["server_stopped"],
            "http_stopped": replay["http_server_stopped"],
            "schema_removed": replay["schema_removed"],
        },
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "new_source_requests": 0,
        "default_method": "fixed",
        "conclusion": "No demonstrated benefit for this single-agent implementation under the tested constraints; retain the fixed default.",
        "limits": [
            "Two previously inspected development cases; prompt repairs are not preregistered or unseen evaluation.",
            "The first round is the same-envelope comparison; later rounds are bounded engineering repairs with lower remaining call/output allowances.",
            "One original invalid-JSON response was not retained by the old adapter. Its usage cannot be reconstructed.",
            "Selected agent outputs are insufficient or rejected; lower tokens/latency cannot imply improved efficiency at equal answer quality.",
            "Implementation-agent reviews are drafts, not independent blinded human judgments.",
        ],
    }
    output = Path("docs/experiments/P1-09-results.json")
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": output.as_posix(),
                "actual_requests": len(ledger["entries"]),
                "usage_complete": not unknown,
                "formal_semantic_metrics": None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

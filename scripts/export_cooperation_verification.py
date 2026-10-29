"""Export observed P1-12 engineering outcomes without source text or provider credentials."""

import json
from pathlib import Path

from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def read(path):
    return json.loads(Path(path).read_bytes())


def runs_summary(comparison):
    result = []
    for run in comparison["runs"]:
        result.append(
            {
                key: run.get(key)
                for key in (
                    "case_id",
                    "method",
                    "task_state",
                    "execution_status",
                    "disposition",
                    "model_calls",
                    "tool_calls",
                    "elapsed_ms",
                    "resources",
                    "delegation_policy",
                    "branch_results",
                    "branch_effective_configs",
                    "branch_timing",
                    "conflicts",
                    "tool_batches",
                    "published_report",
                    "report_content_sha256",
                    "report_path",
                )
            }
        )
    return result


def main():
    root = Path(".local/P1-12")
    offline, database = read(root / "offline-final.json"), read(root / "final.json")
    assert database["test_filter"] is None
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert database["tests"]["failures"] == database["tests"]["errors"] == 0
    assert database["server_stopped"] and database["cooperation_check_exit_code"] == 0
    assert database["agent_check_exit_code"] == 0
    comparison, replay = database["cooperation_real"], database["single_agent"]
    assert comparison["schema_removed"]
    assert len(comparison["runs"]) == 8
    assert comparison["new_api_requests"] == comparison["new_source_requests"] == 0
    assert replay["replay_outcomes_match"] and replay["external_model_calls"] == 0
    assert replay["http_server_stopped"] and replay["schema_removed"]
    for run in comparison["runs"]:
        assert run["task_state"] == "COMPLETED" and run["execution_status"] == "completed"
        ref = run["published_report"]
        report = ArtifactStore(Path(ref["artifact_root"])).read_json(
            ref["report_artifact"]["sha256"]
        )
        assert content_hash(report) == run["report_content_sha256"]
        assert report == read(run["report_path"])
        assert run["model_calls"] <= 8 and run["tool_calls"] <= 10
        # Recorded branch exhaustion/failures remain observations, never silently
        # promoted to successful role completion by the parent's delivery status.
        run["branch_terminations"] = {}
        for branch in report.get("branch_results", []):
            if branch["report_artifact"]:
                saved = ArtifactStore(
                    Path(report["branch_artifact_roots"][branch["delegation_id"]])
                ).read_json(branch["report_artifact"]["sha256"])
                run["branch_terminations"][branch["delegation_id"]] = saved.get("termination")
    ledger = read(".local/P1-09/request-allowance.json")
    prior = read("docs/experiments/P1-09-results.json")
    assert content_hash(ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    history = []
    for name in ("initial.json", "round2.json", "round3.json", "round4.json"):
        value = read(root / name)
        history.append(
            {
                "record": name,
                "tests": value["tests"],
                "test_filter": value["test_filter"],
                "content_sha256": content_hash(value),
                "server_stopped": value["server_stopped"],
            }
        )
    original = read(root / "development-comparison.json")
    diagnostic = read(root / "diagnostic.json")
    retained = read(root / "rounds/pre-storage-fix/index.json")
    assert len(retained) == 9
    for item in retained:
        value = read(item["retained_path"])
        assert content_hash(value) == item["report_content_sha256"]
    value = {
        "schema_version": "P1-12-delivery/0.1",
        "timestamp": database["timestamp"],
        "classification": "real_postgresql_and_threads_retained_official_sources_local_models_scripted_generation",
        "offline_tests": offline["tests"],
        "postgres_tests": database["tests"],
        "offline_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "development_history": history,
        "before_storage_fix": runs_summary(original["cooperation_real"]),
        "diagnostic": runs_summary(diagnostic["cooperation_real"]),
        "before_fix_immutable_reports_retained": retained,
        "common_profile": comparison["profile"],
        "final_run_group_id": comparison["run_group_id"],
        "final_runs": [
            r | {"branch_terminations": original_run["branch_terminations"]}
            for r, original_run in zip(runs_summary(comparison), comparison["runs"], strict=True)
        ],
        "durable_calls": comparison["durable_calls"],
        "legacy_model_responses_replayed": replay["recorded_response_replays"],
        "legacy_outcomes_match": True,
        "new_api_requests": 0,
        "new_source_requests": 0,
        "actual_new_provider_usage": None,
        "estimated_cost": None,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "P1_09_request_ledger_unchanged": True,
        "temporary_postgres_stopped": True,
        "temporary_http_stopped": True,
        "experiment_schemas_removed": True,
        "record_content_sha256": {
            "offline": content_hash(offline),
            "database": content_hash(database),
            "request_ledger": content_hash(ledger),
            "development_comparison": content_hash(original),
            "diagnostic": content_hash(diagnostic),
        },
        "limits": [
            "Scripted generation and synthetic usage are not real model quality or provider cost measurements.",
            "Previously seen two-case development execution is not an unseen test or repeated latency study.",
            "The fixed method remains the default; no quality or speed superiority is established.",
            "Current conflicts conservatively remain unresolved after source replay, pending semantic review.",
            "Role quotas partition one root envelope and do not borrow unused sibling capacity.",
            "Local Qdrant clients serialize directory access; no multi-process backend throughput claim.",
            "In-flight threads/provider requests cannot be forcibly stopped; late results cannot publish.",
            "Lane recovery tests inject a crash in-process; the earlier P1-11 real process experiment is separate.",
        ],
    }
    target = Path("docs/verification/2026-10-27-P1-12-cooperation.json")
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "new_api_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

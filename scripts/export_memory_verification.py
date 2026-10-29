"""Export observed P1-13 engineering checks, not simulated quality or provider usage."""

import hashlib
import json
from pathlib import Path

from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def read(path):
    return json.loads(Path(path).read_bytes())


def main():
    root = Path(".local/P1-13")
    offline, database = read(root / "offline-final.json"), read(root / "final.json")
    assert database["test_filter"] is None and database["pytest_exit_code"] == 0
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert database["tests"]["failures"] == database["tests"]["errors"] == 0
    assert database["server_stopped"] and database["memory_check_exit_code"] == 0
    assert database["agent_check_exit_code"] == 0
    demonstration = database["memory_real"] | {"generation_usage_is_synthetic": True}
    replay = database["single_agent"]
    assert demonstration["schema_removed"] and demonstration["extractor_process_exit_code"] == 0
    assert demonstration["queued_candidates_only_until_review"]
    assert (
        demonstration["active_index_after_deletion"] == 2 and demonstration["adoption_records"] == 5
    )
    assert demonstration["new_api_requests"] == demonstration["new_source_requests"] == 0
    assert (
        demonstration["historical_report_marks_deletion"]
        and demonstration["new_task_omits_deleted_preference"]
    )
    assert [r["memory_references"] for r in demonstration["runs"]] == [3, 2]
    for run in demonstration["runs"]:
        ref = run["published_report"]
        report = ArtifactStore(Path(ref["artifact_root"])).read_json(
            ref["report_artifact"]["sha256"]
        )
        assert content_hash(report) == run["report_content_sha256"]
        assert len(report["memory_usage"]["references"]) == run["memory_references"]
        assert report["execution_status"] == "completed"
        assert report["generation_mode"] == "simulated_model"
    assert replay["replay_outcomes_match"] and replay["external_model_calls"] == 0
    assert replay["http_server_stopped"] and replay["schema_removed"]
    ledger = read(".local/P1-09/request-allowance.json")
    prior = read("docs/experiments/P1-09-results.json")
    assert content_hash(ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    history = []
    for name in ("initial.json", "round2.json"):
        value = read(root / name)
        history.append(
            {
                "record": name,
                "tests": value["tests"],
                "test_filter": value["test_filter"],
                "server_stopped": value["server_stopped"],
                "content_sha256": content_hash(value),
            }
        )
    value = {
        "schema_version": "P1-13-delivery/0.1",
        "timestamp": database["timestamp"],
        "classification": "real_postgresql_review_cache_scope_fault_tests_real_extractor_process_official_sources_simulated_generation",
        "migration_head": "0007_semantic_memory",
        "offline_tests": offline["tests"],
        "postgres_tests": database["tests"],
        "offline_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "development_history": history,
        "first_offline_failure": {
            "tests": 391,
            "failures": 1,
            "reason": "legacy API fixture lacked eagerly accessed memory service; dependency now resolved per request",
            "log_sha256": hashlib.sha256(
                (root / "history/offline-first-failure.log").read_bytes()
            ).hexdigest(),
        },
        "before_verification_deduplication": read(root / "round2.json")["memory_real"],
        "official_demonstration": demonstration,
        "legacy_model_responses_replayed": replay["recorded_response_replays"],
        "legacy_outcomes_match": True,
        "new_api_requests": 0,
        "new_source_requests": 0,
        "extraction_model_calls": 0,
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
        },
        "limits": [
            "The extractor parses bounded explicit declarations; no general natural-language or LLM extraction is claimed.",
            "Only installed operating rules have bounded executable checks; they are not official facts or universal semantic proofs.",
            "Official source bytes/tools and extractor process are real; preferences/reviewers/model generation are controlled fixtures.",
            "Memory pointers cannot become E evidence or widen task authorization.",
            "Deletion stops active adoption but preserves audit/history; it is not physical erasure of all past bytes.",
            "A project namespace belongs to one authenticated owner; team membership is not implemented.",
            "Word matching is not an evaluated semantic/vector retrieval benchmark.",
            "Generation/publication validate current domain sources; step gates validate live primary memory status.",
            "Unknown provider cancellation and changed-memory recovery remain strict, without automatic re-generation.",
            "The official demonstration uses a 32000-character prompt cap and is not an equal-budget benefit comparison.",
        ],
    }
    target = Path("docs/verification/2026-10-27-P1-13-memory.json")
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "new_api_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

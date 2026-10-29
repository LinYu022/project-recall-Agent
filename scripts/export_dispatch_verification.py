"""Publish observed P1-11 engineering outcomes, keeping simulation and real calls separate."""

import json
from pathlib import Path

from ingestion.records import content_hash


def main():
    root = Path(".local/P1-11")
    offline = json.loads((root / "offline-final.json").read_bytes())
    database = json.loads((root / "final.json").read_bytes())
    assert database["test_filter"] is None
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert database["tests"]["failures"] == database["tests"]["errors"] == 0
    assert (
        database["server_stopped"]
        and database["dispatch_check_exit_code"] == 0
        and database["agent_check_exit_code"] == 0
    )
    process = database["dispatch_real"]
    assert process["schema_removed"] and process["successful_publications"] == 1
    assert process["new_api_requests"] == process["new_source_requests"] == 0
    assert process["first_process_exit_code"] == 23 and process["second_process_exit_code"] == 0
    replay = database["single_agent"]
    assert replay["replay_outcomes_match"] and replay["external_model_calls"] == 0
    assert replay["http_server_stopped"] and replay["schema_removed"]
    ledger = json.loads(Path(".local/P1-09/request-allowance.json").read_bytes())
    prior = json.loads(Path("docs/experiments/P1-09-results.json").read_bytes())
    assert content_hash(ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    value = {
        "schema_version": "P1-11-delivery/0.1",
        "timestamp": database["timestamp"],
        "classification": "real_postgresql_fault_injection_real_worker_process_restart_official_sources_simulated_generation",
        "migration_head": "0006_durable_dispatch",
        "offline_tests": offline["tests"],
        "postgres_tests": database["tests"],
        "offline_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "process_experiment": process,
        "legacy_model_responses_replayed": replay["recorded_response_replays"],
        "legacy_outcomes_match": True,
        "new_api_requests": 0,
        "new_source_requests": 0,
        "actual_new_provider_usage": None,
        "estimated_cost": None,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "temporary_postgres_stopped": True,
        "temporary_http_stopped": True,
        "experiment_schemas_removed": True,
        "P1_09_request_ledger_unchanged": True,
        "record_content_sha256": {
            "offline": content_hash(offline),
            "database": content_hash(database),
            "request_ledger": content_hash(ledger),
        },
        "limits": [
            "Queue delivery and readonly retry are at least once; no cross-provider exactly-once guarantee.",
            "Actual process exit/restart uses a simulated abstention model; it is not a quality or API-cost experiment.",
            "Main service objects are reconstructed in the parent process, not a measured full host reboot.",
            "Lease expiry and retry availability are advanced by explicit fault injection, not wall-clock waits.",
            "Completion units without usage are conservatively charged and marked pending reconciliation; they are not measured provider tokens.",
            "An unknown model response is not automatically regenerated, even after accounting reconciliation.",
            "File writes and database settlement have no common transaction; missing proof stops recovery.",
            "Local worker throughput and provider cancellation are not guaranteed by this verification.",
        ],
    }
    target = Path("docs/verification/2026-10-27-P1-11-dispatch.json")
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "new_api_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

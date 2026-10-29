"""Publish distinct full-regression, replay and official-history observations without provider calls."""

import json
from pathlib import Path

from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def read(path):
    return json.loads(Path(path).read_bytes())


def check_reports(demonstration):
    for run in demonstration["runs"]:
        ref = run["report_reference"]
        report = ArtifactStore(Path(ref["artifact_root"])).read_json(
            ref["report_artifact"]["sha256"]
        )
        assert content_hash(report) == run["report_content_sha256"]
        assert report["model_identity"]["is_simulated"]


def main():
    root = Path(".local/P1-14")
    offline, database = read(root / "offline-final.json"), read(root / "postgres-final.json")
    replay_round, official = read(root / "full-round1.json"), read(root / "official-final.json")
    assert database["test_filter"] is None and database["pytest_exit_code"] == 0
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert database["tests"]["failures"] == database["tests"]["errors"] == 0
    assert database["server_stopped"] and official["server_stopped"]
    assert official["pytest_exit_code"] == 0 and official["episode_check_exit_code"] == 0
    demonstration, replay = official["episodes_real"], replay_round["single_agent"]
    assert replay_round["agent_check_exit_code"] == 0 and replay["replay_outcomes_match"]
    assert replay["external_model_calls"] == 0 and replay["recorded_response_replays"] == 8
    assert (
        replay["http_server_stopped"]
        and replay["schema_removed"]
        and replay_round["server_stopped"]
    )
    assert (
        demonstration["schema_removed"]
        and demonstration["wrong_year_excluded"]
        and demonstration["failed_task_retained"]
    )
    assert demonstration["episodes"] == 4 and demonstration["adoption_records"] == 2
    assert demonstration["historical_source_verification"]["status"] == "verified"
    assert demonstration["new_api_requests"] == demonstration["new_source_requests"] == 0
    check_reports(demonstration)
    check_reports(replay_round["episodes_real"])
    ledger, prior = (
        read(".local/P1-09/request-allowance.json"),
        read("docs/experiments/P1-09-results.json"),
    )
    assert content_hash(ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    value = {
        "schema_version": "P1-14-delivery/0.1",
        "timestamp": official["timestamp"],
        "classification": "real_postgresql_history_navigation_and_official_sources_with_scripted_generation",
        "migration_head": "0008_episodic_memory",
        "offline_tests": offline["tests"],
        "postgres_tests": database["tests"],
        "official_followup_tests": official["tests"],
        "official_followup_filter": official["test_filter"],
        "offline_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "development_history": [
            {
                "record": name,
                "tests": read(root / name)["tests"],
                "content_sha256": content_hash(read(root / name)),
            }
            for name in ["initial.json", "round2.json"]
        ],
        "first_official_followup_failure": {
            "full_postgres_tests": replay_round["tests"],
            "official_script_exit_code": replay_round["episode_check_exit_code"],
            "reason": "A literal brake query was incorrectly expected to enumerate braking-only summaries; final harness enumerates eligible history with an empty query.",
            "all_four_task_outcomes_recorded": True,
            "retained_observations": replay_round["episodes_real"],
        },
        "official_demonstration": demonstration,
        "legacy_model_responses_replayed": 8,
        "legacy_outcomes_match": True,
        "P1_09_request_ledger_unchanged": True,
        "new_api_requests": 0,
        "new_source_requests": 0,
        "summary_model_calls": 0,
        "actual_new_provider_usage": None,
        "estimated_cost": None,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "temporary_postgres_stopped": True,
        "temporary_http_stopped": True,
        "experiment_schemas_removed": True,
        "record_content_sha256": {
            "offline": content_hash(offline),
            "database": content_hash(database),
            "replay_round": content_hash(replay_round),
            "official": content_hash(official),
            "request_ledger": content_hash(ledger),
        },
        "limits": [
            "Historical hypotheses/outcomes are model proposals and execution observations, not independent semantic truth.",
            "Current source verification and current E evidence are required; historical IDs cannot become current evidence aliases.",
            "The event window uses recorded section/array order; absolute concurrent chronology is not reconstructed.",
            "History retrieval uses literal word/character matching with no stemming or evaluated vector retrieval benefit.",
            "Seed search is server context preparation; its separate history_preparation cost is not included in runner total_elapsed_ms.",
            "Official sources, SQL and tools are real; generated responses and negative hypothesis labels are scripted.",
            "The demonstration uses a 32000-character prompt cap and is not an equal-budget memory benefit comparison.",
            "The full database suite, saved-response replay and official followup are separate recorded verification phases.",
        ],
    }
    target = Path("docs/verification/2026-10-28-P1-14-episodes.json")
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "new_api_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Export observed P1-10 checks, without synthetic claims about provider answer quality."""

import json
from pathlib import Path

from ingestion.records import content_hash


def main():
    root = Path(".local/P1-10")
    offline = json.loads((root / "offline-final.json").read_bytes())
    database = json.loads((root / "regression-final.json").read_bytes())
    current_database = json.loads((root / "postgres-current.json").read_bytes())
    comparison = json.loads((root / "context-comparison.json").read_bytes())
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert database["tests"]["failures"] == database["tests"]["errors"] == 0
    assert current_database["tests"]["failures"] == current_database["tests"]["errors"] == 0
    assert current_database["server_stopped"] and current_database["pytest_exit_code"] == 0
    replay = database["single_agent"]
    assert database["server_stopped"] and replay["http_server_stopped"] and replay["schema_removed"]
    assert database["agent_check_exit_code"] == 0 and replay["replay_outcomes_match"]
    assert replay["external_model_calls"] == comparison["new_api_requests"] == 0
    assert comparison["official"]["original_evidence_replays"] == 24
    value = {
        "schema_version": "P1-10-delivery/0.1",
        "timestamp": current_database["timestamp"],
        "classification": "real_database_synthetic_models_and_archived_official_source_context_checks_no_new_generation",
        "offline_tests": offline["tests"],
        "postgres_tests": current_database["tests"],
        "offline_check_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "legacy_real_response_replays": replay["recorded_response_replays"],
        "legacy_outcomes_match": replay["replay_outcomes_match"],
        "postgres_stopped": database["server_stopped"],
        "http_stopped": replay["http_server_stopped"],
        "schema_removed": replay["schema_removed"],
        "context_comparison": comparison,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "new_api_requests": 0,
        "new_source_requests": 0,
        "summary_model_calls": 0,
        "estimated_provider_cost": None,
        "record_content_sha256": {
            "offline": content_hash(offline),
            "database": content_hash(database),
            "current_database": content_hash(current_database),
            "comparison": content_hash(comparison),
        },
        "limits": [
            "Summary is deterministic extraction and exact block deduplication, not abstractive LLM generation.",
            "Condition/negation heuristics conservatively pin complete blocks, but are not a semantic completeness oracle.",
            "The official next input is reconstructed from P1-09 state, not submitted to a model.",
            "Context fits and source replay do not prove answer quality gains.",
            "Revision-bound context loading does not implement worker restart or task resume; P1-11 remains pending.",
        ],
    }
    output = Path("docs/verification/2026-10-27-P1-10-context.json")
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": output.as_posix(), "new_api_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

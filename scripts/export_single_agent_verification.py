"""Export the historical pre-live snapshot; current results use export_single_agent_results."""

import json
from pathlib import Path

from ingestion.records import content_hash


def main():
    root = Path(".local/P1-09")
    offline = json.loads((root / "offline-final.json").read_bytes())
    database = json.loads((root / "official-simulated.json").read_bytes())
    comparison = database["single_agent"]
    assert database["agent_check_exit_code"] == 0 and comparison["external_model_calls"] == 0
    assert offline["tests"]["errors"] == offline["tests"]["failures"] == 0
    assert database["tests"]["errors"] == database["tests"]["failures"] == 0
    value = {
        "schema_version": "P1-09-engineering-verification/0.1",
        "timestamp": database["timestamp"],
        "classification": "real_postgresql_official_frozen_sources_scripted_decisions_and_simulated_answers",
        "offline_tests": offline["tests"],
        "postgres_tests": database["tests"],
        "offline_check_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "comparison_profile": comparison["profile"],
        "runs": comparison["runs"],
        "external_model_calls": 0,
        "actual_real_model_usage": None,
        "formal_semantic_metrics": None,
        "live_comparison_status": "awaiting_explicit_data_transmission_approval",
        "approval_review": {
            "action": "P1-09 paired DeepSeek experiment with two saved public NHTSA cases",
            "status": "rejected",
            "reason": "Existing explicit external-call authorization was judged to cover P1-07, not these P1-09 data transmissions.",
            "calls_sent": 0,
            "workaround_used": False,
        },
        "http_server_stopped": comparison["http_server_stopped"],
        "schema_removed": comparison["schema_removed"],
        "postgres_stopped": database["server_stopped"],
        "new_source_requests": 0,
        "record_content_sha256": {
            "offline": content_hash(offline),
            "database": content_hash(database),
        },
        "limits": [
            "Scripted branches demonstrate mechanics, not actual model reasoning or relative answer quality.",
            "Synthetic token counters in fixture responses are not real provider usage or cost.",
            "P1-07/P1-08 semantic review remains pending; no independent human review is fabricated.",
            "Background recovery, advanced context summaries and worker budgets remain later modules.",
        ],
    }
    target = Path("docs/verification/2026-10-27-P1-09-engineering.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "live_model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

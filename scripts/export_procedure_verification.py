"""Export checked engineering observations, never infer real model quality from scripted steps."""

import hashlib
import json
from pathlib import Path

from ingestion.records import content_hash
from memory.procedures import engine_fingerprint
from runtime.artifacts import ArtifactStore


def read(path):
    return json.loads(Path(path).read_bytes())


def check_demonstration(demo):
    assert "failure_type" not in demo
    assert demo["schema_removed"] and demo["new_api_requests"] == demo["new_source_requests"] == 0
    assert demo["validation"]["state"] == "passed" and demo["validation"]["tool_calls"] == 3
    assert demo["validation_new_model_calls"] == 0
    assert demo["deleted_precondition_simulation"]["compiler_blocked"]
    assert demo["final_skill_state"] == "revoked"
    assert demo["post_revoke_report_audit"]["state"] == "unavailable"
    for run in demo["runs"]:
        ref = run["report_reference"]
        report = ArtifactStore(Path(ref["artifact_root"])).read_json(
            ref["report_artifact"]["sha256"]
        )
        assert content_hash(report) == run["report_content_sha256"]
        assert report["model_identity"]["is_simulated"]
        assert report["evidence_verification"]["status"] == "verified"
    pointer = demo["validation"]["proof"]
    proof = ArtifactStore(Path(pointer["artifact_root"])).read_json(pointer["artifact"]["sha256"])
    assert proof["engine_sha256"] == engine_fingerprint()
    assert proof["validation_state"] == "passed"
    for transfer in proof["transfers"]:
        saved = ArtifactStore(Path(transfer["artifact_root"])).read_json(
            transfer["artifact"]["sha256"]
        )
        assert saved["result"] == transfer["result"]
        assert content_hash(saved["cards"]) == saved["result"]["cards_sha256"]


def main():
    root = Path(".local/P1-15")
    offline, database = read(root / "offline-final.json"), read(root / "final.json")
    assert database["test_filter"] is None and database["pytest_exit_code"] == 0
    assert database["tests"]["failures"] == database["tests"]["errors"] == 0
    assert offline["tests"]["failures"] == offline["tests"]["errors"] == 0
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert database["server_stopped"] and database["procedure_check_exit_code"] == 0
    assert database["agent_check_exit_code"] == 0
    replay = database["single_agent"]
    assert replay["replay_outcomes_match"] and replay["recorded_response_replays"] == 8
    assert (
        replay["external_model_calls"] == 0
        and replay["http_server_stopped"]
        and replay["schema_removed"]
    )
    demonstration = database["procedures_real"]
    check_demonstration(demonstration)
    ledger = read(".local/P1-09/request-allowance.json")
    prior = read("docs/experiments/P1-09-results.json")
    assert content_hash(ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    plan_hash = hashlib.sha256(
        Path("docs/plan/implementation-plan-v2.0.md").read_bytes()
    ).hexdigest()
    assert plan_hash == "04b03e1cfe4a5d855608a970eb6e5a097e8fae34a9ed52742af941b836f4a5a7"
    development = []
    failed_official = []
    for name in ("initial.json", "round2.json", "round3.json", "round4.json", "round5.json"):
        path = root / name
        if path.exists():
            record = read(path)
            development.append(
                {
                    "record": name,
                    "tests": record["tests"],
                    "test_filter": record["test_filter"],
                    "server_stopped": record["server_stopped"],
                    "procedure_check_exit_code": record.get("procedure_check_exit_code"),
                    "content_sha256": content_hash(record),
                }
            )
            if (demo := record.get("procedures_real")) and "failure_type" in demo:
                recovered = []
                for run in demo["runs"]:
                    ref = run["report_reference"]
                    if "artifact_root" not in ref:
                        matches = list(Path(demo["root"]).rglob(ref["sha256"] + ".json"))
                        assert len(matches) == 1
                        ref = {
                            "artifact_root": matches[0].parent.parent.as_posix(),
                            "report_artifact": {"sha256": ref["sha256"]},
                        }
                    raw = ArtifactStore(Path(ref["artifact_root"])).read_json(
                        ref["report_artifact"]["sha256"]
                    )
                    recovered.append(
                        {
                            "task_id": run["task_id"],
                            "reference": ref,
                            "raw_content_sha256": content_hash(raw),
                            "termination": raw.get("termination"),
                        }
                    )
                failed_official.append(
                    {"record": name, "observations": demo, "recovered_original_reports": recovered}
                )
    value = {
        "schema_version": "P1-15-delivery/0.1",
        "timestamp": database["timestamp"],
        "classification": "real_postgresql_bounded_procedure_transfer_official_sources_scripted_generation",
        "migration_head": "0009_procedural_memory",
        "engine_sha256": engine_fingerprint(),
        "offline_tests": offline["tests"],
        "postgres_tests": database["tests"],
        "offline_exit_codes": [c["exit_code"] for c in offline["checks"]],
        "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        "development_history": development,
        "failed_official_rounds": failed_official,
        "official_demonstration": demonstration,
        "legacy_model_responses_replayed": 8,
        "legacy_outcomes_match": True,
        "P1_09_request_ledger_unchanged": True,
        "new_api_requests": 0,
        "new_source_requests": 0,
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
            "request_ledger": content_hash(ledger),
            "original_plan": plan_hash,
        },
        "limits": [
            "Success means completed verifiable scope-inspection workflow, not semantically correct final answers.",
            "Known related campaigns are grouped conservatively; unknown family relationships are not inferred.",
            "Sources, tools, SQL and evidence replay are real; generation and failure injection are scripted.",
            "Text-versus-Skill action counts are predetermined and do not establish quality cost latency or general skill benefits.",
            "No arbitrary workflow code is executed; the installed recipe is deliberately bounded.",
            "Validation crash checks are same-process fault injection, not a new independent-process kill experiment.",
            "Historical test failures remain retained; final counts do not relabel them as passing.",
        ],
    }
    target = Path("docs/verification/2026-10-28-P1-15-procedures.json")
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "new_api_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

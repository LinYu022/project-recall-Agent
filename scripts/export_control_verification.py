"""Export bounded validation metadata; keep local credentials and private report artifacts out."""

import json
from pathlib import Path

from ingestion.records import content_hash


def main():
    root = Path(".local/P1-08")
    offline = json.loads((root / "offline-final-v2.json").read_bytes())
    database = json.loads((root / "postgres-final-v2.json").read_bytes())
    release = json.loads((root / "postgres-final.json").read_bytes())
    control = database["control_real"]
    assert offline["tests"]["failures"] == database["tests"]["failures"] == 0
    assert offline["tests"]["errors"] == database["tests"]["errors"] == 0
    assert database["control_check_exit_code"] == 0
    assert release["release_check_exit_code"] == 0
    assert (
        control["external_model_calls"] == 0
        and control["http_server_stopped"]
        and control["schema_removed"]
    )
    result = {
        "schema_version": "P1-08-verification/0.1",
        "timestamp": database["timestamp"],
        "classification": "real_postgresql_and_http_with_synthetic_permissions_and_generation_plus_saved_official_data",
        "offline": {
            "tests": offline["tests"],
            "check_exit_codes": [c["exit_code"] for c in offline["checks"]],
            "legacy_http_checks": [offline["http_checks"], offline["query_http_checks"]],
        },
        "postgres": {
            "tests": database["tests"],
            "server_stopped": database["server_stopped"],
            "cases": database["cases"],
        },
        "release_regression": {
            "checks": len(release["release_real"]["checks"]),
            "http_stopped": release["release_real"]["http_server_stopped"],
            "schema_removed": release["release_real"]["disposable_schema_removed"],
        },
        "protected_official_data_flow": control,
        "migration": "0005_task_control",
        "new_dependencies": 0,
        "external_model_calls": 0,
        "new_source_requests": 0,
        "formal_semantic_metrics": None,
        "record_content_sha256": {
            "offline": content_hash(offline),
            "database": content_hash(database),
            "release": content_hash(release),
        },
        "limits": [
            "User actions and malicious PDF content are synthetic fixtures, not observed attacks.",
            "Real official sources and real PostgreSQL/HTTP do not make synthetic answers real model experiments.",
            "Persistent lifecycle is implemented; background workers, leases and automatic continuation remain P1-11.",
            "Trusted local process/database/file administrator is outside remote API authorization threat model.",
            "P1-07 independent semantic review remains pending.",
        ],
    }
    target = Path("docs/verification/2026-10-27-P1-08-control.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": target.as_posix(), "external_model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

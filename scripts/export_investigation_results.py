"""Publish only bounded result metadata, never raw source narratives, credentials or model inputs."""

import json
from pathlib import Path

from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def main():
    root = Path(".local/P1-07")
    final = json.loads((root / "final-replay.json").read_bytes())
    offline = json.loads((root / "offline-final-v3.json").read_bytes())
    mapping = json.loads((root / "recorded-model/mapping.json").read_bytes())
    store = ArtifactStore(root / "recorded-model")
    responses = [store.read_json(v) for v in mapping.values()]
    runs = []
    for name in (
        "live-baseline.json",
        "live-baseline-v2.json",
        "baseline-completed.json",
        "baseline-v3.json",
        "final-replay.json",
    ):
        record = json.loads((root / name).read_bytes())
        experiment = record["investigation_real"]
        runs.append(
            {
                "record": name,
                "record_content_sha256": content_hash(record),
                "classification": experiment["classification"],
                "new_model_calls": experiment["external_generative_model_calls"],
                "response_replays": experiment["recorded_response_replays"],
                "cases": [
                    {
                        k: c[k]
                        for k in (
                            "case_id",
                            "execution_status",
                            "disposition",
                            "generation_mode",
                            "error_code",
                            "tool_calls",
                            "model_calls",
                            "report_artifact",
                            "report_content_sha256",
                        )
                    }
                    for c in experiment["cases"]
                ],
            }
        )
    draft = (
        json.loads((root / "draft-review-summary.json").read_bytes())
        if (root / "draft-review-summary.json").exists()
        else []
    )
    result = {
        "schema_version": "P1-07-public-results/0.1",
        "timestamp": final["timestamp"],
        "classification": "previously_seen_development_only_not_independent_human_or_unseen_test",
        "model_identity": final["investigation_real"]["model_identity"],
        "config": final["investigation_real"]["config"],
        "new_source_requests": 0,
        "total_unique_real_model_responses": len(responses),
        "total_real_usage": {
            k: sum(r["usage"][k] for r in responses)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "estimated_cost": None,
        "rounds": runs,
        "verification": {
            "offline_tests": offline["tests"],
            "postgres_tests": final["tests"],
            "offline_check_exit_codes": [c["exit_code"] for c in offline["checks"]],
            "old_http_checks": [offline["http_checks"], offline["query_http_checks"]],
            "published_release_real_checks": len(final["release_real"]["checks"]),
            "investigation_http_cases": len(final["investigation_real"]["cases"]),
            "investigation_boundary_checks": final["investigation_real"]["http_checks"],
            "final_new_model_calls": final["investigation_real"]["external_generative_model_calls"],
            "final_response_replays": final["investigation_real"]["recorded_response_replays"],
            "postgres_stopped": final["server_stopped"],
            "http_stopped": final["investigation_real"]["http_server_stopped"],
            "schema_removed": final["investigation_real"]["disposable_schema_removed"],
        },
        "draft_reviews": draft,
        "independent_human_reviews": 0,
        "formal_semantic_metrics": None,
        "limits": [
            "Draft reviews are implementation-agent judgments, not independent human gold labels.",
            "Selected API responses are replayable; cloud regeneration is not guaranteed byte-identical.",
            "Reference v0.2 is an explicit post-output development correction, not preregistered test evaluation.",
            "GitHub upload and clean-environment final reproduction remain later modules.",
        ],
    }
    path = Path("docs/experiments/P1-07-results.json")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"output": path.as_posix(), "new_model_calls": 0, "formal_semantic_metrics": None}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

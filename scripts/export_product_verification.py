"""Bind product delivery to actual browser, database, source and recording artifacts."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from evaluation.freeze import load
from ingestion.records import content_hash
from portal.identity import implementation


def read(path):
    return json.loads(Path(path).read_bytes())


def fingerprint(path):
    path = Path(path)
    return {"path": path.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", type=Path, required=True)
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--postgres", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    product_identity = implementation()
    original = read(".local/P1-16/active-experiment.json")
    load(original)
    previous = read("docs/verification/2026-10-29-P1-16-delivery.json")
    for item in previous["source_fingerprints"].values():
        assert content_hash(read(item["path"])) == item["content_sha256"]
    browsers = []
    for path in (args.synthetic, args.official):
        record = read(path)
        assert record["status"] == "passed" and len(record["checks"]) == 19
        assert not record["errors"] and record["new_external_model_calls"] == 0
        assert record["new_source_requests"] == 0 and not record["second_human_user"]
        assert record["runtime_info"]["implementation_sha256"] == product_identity
        assert record["runtime_info"]["model_quality_acceptance"] == "not_established"
        assert record["artifacts"]["failure"]["task"]["state"] == "PAUSED"
        assert (
            record["artifacts"]["failure"]["dispatch"]["jobs"][-1]["last_error"]
            == "unknown_model_output"
        )
        assert record["artifacts"]["relationship"]["result"]
        assert record["artifacts"]["original"]["locations"][0]["status"] == "verified"
        video = path.parent / record["video"]["file"]
        video_fp = fingerprint(video)
        assert video_fp["sha256"] == record["video"]["sha256"]
        browsers.append(
            {
                "profile": record["profile"],
                "checks": record["checks"],
                "source_record": fingerprint(path),
                "video": video_fp,
                "release_id": record["artifacts"]["simple"]["report"]["release_id"],
                "generation": "simulated",
                "independent_human_usability": False,
            }
        )
    offline, postgres = read(args.offline), read(args.postgres)
    for record in (offline, postgres):
        assert not any(record["tests"][key] for key in ("failures", "errors", "skipped"))
    assert postgres["server_stopped"]
    value = {
        "module": "P1-17",
        "timestamp": datetime.now(UTC).isoformat(),
        "engineering_and_demo_delivery_complete": True,
        "all_original_product_acceptance_requirements_passed": False,
        "formal_model_quality_acceptance": "not_established",
        "original_generator_freeze_unchanged": True,
        "P1_16_sources_and_quality_reservations_unchanged": True,
        "new_external_model_calls": 0,
        "new_source_requests": 0,
        "browsers": browsers,
        "offline": fingerprint(args.offline) | {"tests": offline["tests"]},
        "postgres": fingerprint(args.postgres) | {"tests": postgres["tests"]},
        "product_implementation_sha256": product_identity,
        "container_runtime_verified": False,
        "second_human_user_acceptance": "not_performed",
        "remaining_requirements": [
            "Build and start the actual Docker Compose environment, preserving logs.",
            "A second person follows README and identifies a partial failure; agent cannot fill this.",
            "P1-16 prospective quality acceptance remains unestablished; product tests do not replace it.",
        ],
        "P1_18_started": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "browser_checks": [19, 19],
                "offline_tests": offline["tests"],
                "postgres_tests": postgres["tests"],
            }
        )
    )


if __name__ == "__main__":
    main()

"""Audit saved post hoc reviews without SQL, provider calls, or historical rewrites."""

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from ingestion.records import content_hash
from reviewing.corpus import analyze, read, verified, write_new
from runtime.artifacts import native_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--offline-record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pointer = read(args.assessment)
    directory, identity, _, rows, _ = verified(pointer)
    manifest = read(directory / "manifest.json")
    summary = read("docs/verification/2026-10-28-P1-16-role-review-v0.2.json")
    assert analyze(pointer) == summary
    assert read("evals/role-aware-review-v0.2/assessment-manifest.json") == manifest
    assert len(rows) == 270 and sum(r["observation"] is not None for r in rows) == 269
    packets = list(native_path(directory / "packets").glob("*.json"))
    assert len(packets) == 269

    original = read("docs/verification/2026-10-28-P1-16-finalization.json")
    preserved = {}
    for name, expected in [
        ("evaluation", original["raw_verification_content_sha256"]),
        ("analysis-supplement", original["posthoc_analysis_content_sha256"]),
        ("human-review", original["human_supplement_content_sha256"]),
    ]:
        path = "docs/verification/2026-10-28-P1-16-" + name + ".json"
        assert content_hash(read(path)) == expected
        preserved[path] = expected
    old_finalization = subprocess.run(
        ["git", "show", "30a79f7:docs/verification/2026-10-28-P1-16-finalization.json"],
        capture_output=True,
        check=True,
    ).stdout
    assert content_hash(json.loads(old_finalization)) == content_hash(original)
    preserved["docs/verification/2026-10-28-P1-16-finalization.json"] = content_hash(original)
    for figure in original["figures"].values():
        data = Path(figure["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == figure["sha256"]
        assert len(data) == figure["bytes"]
    plan_digest = "04b03e1cfe4a5d855608a970eb6e5a097e8fae34a9ed52742af941b836f4a5a7"
    plan = Path("docs/plan/implementation-plan-v2.0.md").read_bytes()
    assert hashlib.sha256(plan).hexdigest() == plan_digest
    ledger = read(".local/P1-16/provider-request-allowance.json")
    raw = read("docs/verification/2026-10-28-P1-16-evaluation.json")
    assert content_hash(read(".local/P1-16/offline-final.json")) == raw["raw_records"]["offline"]
    assert content_hash(ledger) == raw["raw_records"]["ledger"]
    assert len(ledger["entries"]) == 209
    legacy = read(".local/P1-09/request-allowance.json")
    assert content_hash(legacy) == raw["raw_records"]["legacy_ledger"]
    assert len(legacy["entries"]) == 12

    development = read("docs/experiments/P1-16-development-human-review.json")
    calibration = (
        Path(".local/P1-16/development-review-calibration") / development["calibration_id"]
    )
    historical = read(calibration / "manifest-private.json")
    item = next(i for i in historical["items"] if i["blind_id"] == development["blind_id"])
    for key in ["report_content_sha256", "reference_sha256", "packet_content_sha256"]:
        assert item[key] == development[key]
    packet = read(calibration / "packets" / (development["blind_id"] + ".json"))
    assert content_hash(packet) == development["packet_content_sha256"]
    assert set(development["claim_verdicts"].values()) == {"supported"}
    assert len(development["claim_verdicts"]) == 6
    assert development["task_success"] is True and development["formal_uniform_metrics"] is None
    assert len(historical["items"]) == 4

    offline = read(args.offline_record)
    assert offline == read(offline["immutable_record"])
    assert offline["tests"] == {"tests": 469, "failures": 0, "errors": 0, "skipped": 0}
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert offline["external_model_calls"] == 0
    recheck = read(".local/P1-16/frozen-429-recheck/record.json")
    assert recheck["tests"] == {"tests": 429, "failures": 0, "errors": 0, "skipped": 0}
    history = []
    for name, expected in [
        ("review-v02-offline-final", (469, 7, 0)),
        ("review-deep-path-regression", (40, 0, 10)),
        ("review-deep-path-round2", (40, 0, 0)),
    ]:
        path = Path(".local/P1-16") / name / "tests.xml"
        data = path.read_bytes()
        suite = ElementTree.fromstring(data).find("testsuite")
        counts = {k: int(suite.attrib[k]) for k in ("tests", "failures", "errors", "skipped")}
        assert tuple(counts[k] for k in ("tests", "failures", "errors")) == expected
        history.append(
            {"xml": path.as_posix(), "sha256": hashlib.sha256(data).hexdigest(), **counts}
        )
    reviewed = [r for r in summary["rows"] if r["formal"] is not None]
    assert len(reviewed) == 1 and reviewed[0]["human_task_judgment"] is False
    assert summary["aggregate_quality_metrics"] is None
    packet = read(directory / "packets" / "BLIND-525c27d5cddc.json")
    lookup = next(e for e in packet["evidence"] if e["alias"] == "E001")
    visibility = lookup["input_visibility"]
    assert visibility["status"] == "exact_minimized_structured_payload_seen"
    assert "rows" in visibility["raw_fields_not_in_matched_payload"]
    write_new(
        args.output,
        {
            "schema_version": "P1-16-role-review-integrity/0.2",
            "classification": "post_hoc_read_only_audit_not_new_generation_or_quality_acceptance",
            "assessment_id": pointer["assessment_id"],
            "experiment_id": identity["experiment_id"],
            "analysis_implementation": manifest["analysis_implementation"],
            "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "generator_freeze_load": "passed",
            "frozen_states": dict(Counter(r["state"] for r in rows)),
            "public_summary_content_sha256": content_hash(summary),
            "original_public_records_preserved": preserved,
            "original_figures_bytes_verified": True,
            "original_plan_sha256": plan_digest,
            "P1_16_ledger_content_sha256": content_hash(ledger),
            "P1_16_total_provider_requests": len(ledger["entries"]),
            "P1_09_ledger_content_sha256": content_hash(legacy),
            "P1_09_total_provider_requests": len(legacy["entries"]),
            "new_provider_requests": 0,
            "frozen_denominator": len(rows),
            "blind_packets": len(packets),
            "unobserved_cell_has_packet": False,
            "actual_human_reviews": {"frozen_test": 1, "historical_development": 1},
            "actual_human_reviewed_claims": 12,
            "strict_frozen_test_review": reviewed[0]["formal"],
            "historical_development_qualitative_task_success": True,
            "historical_development_formal_uniform_metrics": None,
            "historical_calibration_manifest_content_sha256": content_hash(historical),
            "historical_review_content_sha256": content_hash(development),
            "offline_record": offline,
            "frozen_429_new_recheck": recheck,
            "old_offline_xml_preserved": False,
            "old_offline_summary_preserved": True,
            "old_offline_summary_content_sha256": raw["raw_records"]["offline"],
            "development_verification_history": history,
            "database_204_checks": "historical_not_rerun_for_review_only_changes",
            "current_system_independent_development_baseline": None,
            "pre_registered_numeric_quality_target": None,
            "population_semantic_quality": None,
            "P1_16_fully_accepted": False,
            "P1_17_started": False,
        },
    )
    print(json.dumps({"status": "verified", "output": args.output.as_posix()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Export full denominators, exact provider artifacts and unresolved human review status."""

import json
from pathlib import Path
from xml.etree import ElementTree

from evaluation.__main__ import analyze
from evaluation.freeze import load
from evaluation.records import atomic_json
from evaluation.render import render
from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def read(path):
    return json.loads(Path(path).read_bytes())


def main():
    root = Path(".local/P1-16")
    identity = read(root / "active-experiment.json")
    body, protocol = load(identity, require_current=False)
    directory = Path(identity["artifact_root"]) / "live"
    analysis = analyze(identity, body, protocol, directory, "live")
    ledger_path = root / "provider-request-allowance.json"
    ledger = read(ledger_path) if ledger_path.exists() else None
    request_count = 0
    if ledger:
        assert ledger["spec"]["experiment_id"] == identity["experiment_id"]
        assert ledger["spec"]["maximum_requests"] == body["maximum_provider_requests"] == 584
        assert len(ledger["entries"]) <= 584
        provider = ArtifactStore(directory / "provider")
        for entry in ledger["entries"]:
            if entry.get("artifact"):
                value = provider.read_json(entry["artifact"]["sha256"])
                request = provider.read_json(value["input_artifact"]["sha256"])
                assert content_hash(request) == entry["fingerprint"]
                assert request["max_tokens"] == entry["max_tokens"]
        request_count = len(ledger["entries"])
    for row in analysis["rows"]:
        if row.get("report_reference"):
            pointer = row["report_reference"]
            report = ArtifactStore(Path(pointer["artifact_root"])).read_json(
                pointer["report_artifact"]["sha256"]
            )
            assert content_hash(report) == row["report_content_sha256"]
            assert not report["model_identity"].get("is_simulated")
    offline = read(root / "offline-final.json")
    engineering = read(root / "final-engineering-round2.json")
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert engineering["pytest_exit_code"] == 0 and engineering["server_stopped"]
    assert engineering["agent_check_exit_code"] == engineering["evaluation_preview_exit_code"] == 0
    assert engineering["single_agent"]["recorded_response_replays"] == 8
    assert engineering["single_agent"]["replay_outcomes_match"]
    assert engineering["single_agent"]["external_model_calls"] == 0
    assert engineering["evaluation_preview"]["external_model_requests"] == 0
    assert engineering["evaluation_preview"]["schema_removed"]
    for preview in engineering["evaluation_preview"]["runs"]:
        store = ArtifactStore(Path(preview["input_root"]))
        for pointer in preview["inputs"]:
            store.read_json(pointer["sha256"])
    suite_paths = [
        Path(engineering["artifacts"]) / "tests.xml",
        Path(offline["logs"]) / "tests.xml",
    ]
    tests = [node for p in suite_paths for node in ElementTree.parse(p).findall(".//testcase")]
    challenge_results = []
    for case in read("evals/comprehensive-v0.1/challenges.json")["cases"]:
        matching = [t for t in tests if t.attrib["name"] == case["test"]]
        challenge_results.append(
            case
            | {
                "observed_test_count": len(matching),
                "status": "passed"
                if matching
                and all(
                    not any(t.find(k) is not None for k in ["failure", "error", "skipped"])
                    for t in matching
                )
                else "missing_or_failed",
            }
        )
    assert all(c["status"] == "passed" for c in challenge_results)
    prior = read("docs/experiments/P1-09-results.json")
    old_ledger = read(".local/P1-09/request-allowance.json")
    assert (
        content_hash(old_ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    )
    figures = render(analysis, Path("docs/experiments/P1-16-figures"))
    history = []
    for path in sorted(root.glob("database-round*.json")):
        record = read(path)
        history.append(
            {"record": path.name, "tests": record["tests"], "content_sha256": content_hash(record)}
        )
    local_status = Path(".local/e16-db") / identity["experiment_id"][:12] / "l/status.json"
    state = read(local_status) if local_status.exists() else None
    value = {
        "schema_version": "P1-16-delivery/0.1",
        "date": "2026-10-28",
        "timezone": "Asia/Shanghai",
        "experiment": identity,
        "protocol_content_sha256": content_hash(protocol.model_dump(mode="json")),
        "status": "runs_finished_pending_independent_review"
        if analysis["summary"]["unresolved_cells"] == 0
        else "partial_or_pending",
        "main_tasks": len(next(a for a in protocol.arms if a.family == "main").cases),
        "campaigns": sum(len(c) for c in protocol.cluster_campaigns.values()),
        "event_clusters": len(protocol.cluster_campaigns),
        "frozen_cells": len(body["cells"]),
        "summary": analysis["summary"],
        "rows": analysis["rows"],
        "new_provider_requests_reserved": request_count,
        "maximum_approved_requests": 584,
        "new_source_requests": 20,
        "actual_provider_prices_verified": False,
        "actual_monetary_cost": None,
        "independent_human_reviews": sum(
            a["independent_reviews"] for a in analysis["summary"]["arms"].values()
        ),
        "offline_tests": offline["tests"],
        "postgres_tests": engineering["tests"],
        "development_history": history,
        "challenge_results": challenge_results,
        "figures": figures,
        "legacy_responses_replayed": 8,
        "P1_09_ledger_unchanged": True,
        "local_execution_status": state,
        "experiment_database_retained_for_audit": True,
        "raw_records": {
            "offline": content_hash(offline),
            "engineering": content_hash(engineering),
            "ledger": content_hash(ledger) if ledger else None,
            "analysis": content_hash(analysis),
            "legacy_ledger": content_hash(old_ledger),
        },
        "limits": analysis["summary"]["limitations"]
        + [
            "References were authored by the implementation agent before generation; independent semantic review remains separate.",
            "Monetary prices are unverified; reported token usage and wall time are separate observations.",
            "Challenge sources and failure injections are synthetic and never counted as natural-task quality observations.",
        ],
    }
    atomic_json(Path("docs/verification/2026-10-28-P1-16-evaluation.json"), value)
    print(
        json.dumps(
            {
                "status": value["status"],
                "new_provider_requests": request_count,
                "unresolved_cells": analysis["summary"]["unresolved_cells"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

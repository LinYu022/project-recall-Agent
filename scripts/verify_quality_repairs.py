"""Audit known seen-development witnesses and historical integrity, without model/database calls."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

from evaluation.freeze import implementation, load
from ingestion.records import content_hash
from investigation.contracts import AnswerProposal, EvidenceCard
from investigation.output_policy import check_answer
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = read(args.baseline)
    archive = args.baseline.parent / "before-repairs-tracked.zip"
    if (
        hashlib.sha256(archive.read_bytes()).hexdigest()
        != baseline["tracked_source_archive_sha256"]
    ):
        raise ValueError("historical_archive_changed")
    crlf_paths = []
    with ZipFile(archive) as old:
        for path, digest in baseline["production_fingerprints"].items():
            raw = old.read(path)
            if hashlib.sha256(raw).hexdigest() != digest:
                # Git archives contain repository line endings. The Windows
                # generation freeze fingerprinted checkout bytes. Recover only
                # if the original frozen byte hash exactly verifies the result.
                checkout = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                if hashlib.sha256(checkout).hexdigest() != digest:
                    raise ValueError("archive_does_not_match_original_generation_freeze:" + path)
                crlf_paths.append(path)

    expected_changed = {
        "control/service.py",
        "investigation/agent.py",
        "investigation/flow.py",
        "investigation/cooperation.py",
    }
    changed = {
        path
        for path, digest in baseline["files"].items()
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
    }
    if changed != expected_changed:
        raise ValueError("unexpected_change_to_frozen_or_human_artifacts")
    current = implementation()
    added = set(current) - set(baseline["production_fingerprints"])
    if added != {"investigation/communication.py", "investigation/output_policy.py"}:
        raise ValueError("unexpected_added_generation_code")

    identity = read(Path(".local/q16/confirmation-freeze-v1/active-confirmation.json"))
    body, _ = load(identity, require_current=False)
    if body["implementation_sha256"] != baseline["production_fingerprints"]:
        raise ValueError("original_freeze_was_modified")
    try:
        load(identity)
    except ValueError as error:
        if str(error) != "experiment_code_changed_requires_new_freeze":
            raise
    else:
        raise ValueError("new_generator_would_reuse_old_confirmation_identity")

    # Existing answers are seen development material. They are never rewritten,
    # regraded as human judgments, or turned into fresh acceptance measurements.
    root = Path(".local/q16/confirmation-live-v1")
    rows = read(root / "runs.json")["rows"]
    store = ArtifactStore(root)
    witnesses = [
        (
            "BLIND-dd150e5633dc",
            "fixed:QTEST-25V065000-FACTS:1",
            "local_text_marker_missing",
            "C002",
        ),
        (
            "BLIND-063f11838e34",
            "fixed:QTEST-25V065000-PAIR:1",
            "unconfirmed_relation_asserted",
            "C005",
        ),
        (
            "BLIND-f8aa3ecd8492",
            "fixed:QTEST-24V104000-PAIR:1",
            "unconfirmed_relation_asserted",
            "C006",
        ),
        (
            "BLIND-1804961441ae",
            "multi_agent:QTEST-24V935000-PAIR:1",
            "requested_source_field_not_cited",
            None,
        ),
        ("BLIND-65b17903280f", "fixed:QTEST-24V935000-FACTS:1", None, None),
    ]
    results = []
    for blind_id, cell_id, expected_code, expected_claim in witnesses:
        # The blind identity must bind to this actual original observation/report.
        packet_path = Path(".local/q16/confirmation-review-final-v1/packets") / (blind_id + ".json")
        packet = read(packet_path)
        observation = store.read_json(rows[cell_id]["record"]["sha256"])
        ref = observation["report_reference"]
        report = ArtifactStore(Path(ref["artifact_root"])).read_json(
            ref["report_artifact"]["sha256"]
        )
        if report["proposal"] != packet["proposal"]:
            raise ValueError("witness_cell_or_proposal_binding_differs")
        checks = check_answer(
            AnswerProposal.model_validate(report["proposal"]),
            [EvidenceCard.model_validate(c) for c in report["cards"]],
            report["aliases"],
            packet["question"],
        )
        if expected_code:
            if not any(
                f["code"] == expected_code
                and (expected_claim is None or f.get("claim_id") == expected_claim)
                for f in checks["findings"]
            ):
                raise ValueError("known_development_failure_was_not_rejected:" + blind_id)
        elif checks["findings"]:
            raise ValueError("positive_development_control_was_rejected:" + blind_id)
        results.append(
            {
                "blind_id": blind_id,
                "cell_id": cell_id,
                "observation_sha256": content_hash(observation),
                "report_sha256": content_hash(report),
                "checks": checks,
                "classification": "seen_development_replay_not_new_quality_score",
            }
        )
    result = {
        "schema_version": "quality-repair-development-verification/0.1",
        "base_commit": baseline["base_commit"],
        "prior_experiment_id": identity["experiment_id"],
        "prior_acceptance_status": "not_passed",
        "historical_source_archive_sha256": baseline["tracked_source_archive_sha256"],
        "historical_frozen_production_matches_archive": True,
        "archive_paths_requiring_verified_crlf_checkout": crlf_paths,
        "old_identity_rejected_for_new_generator": True,
        "original_results_human_reviews_wrappers_sources_and_ledgers_unchanged": True,
        "intentionally_changed_production_files": sorted(changed),
        "new_production_files": sorted(added),
        "current_generation_fingerprints": current,
        "seen_development_witnesses": results,
        "new_provider_calls": 0,
        "fresh_confirmatory_quality_measured": False,
        "limitations": [
            "Narrow lexical/source-field publication guards do not establish arbitrary semantic entailment.",
            "Malformed decisions remain rejected; guidance is improved without relaxing authorization, coercing fields or adding retries.",
            "No new unseen confirmation, atomic grades or acceptance pass is inferred.",
        ],
    }
    write_new(args.output, result)
    print(
        json.dumps(
            {
                "status": "verified_seen_development_only",
                "witnesses": len(results),
                "new_provider_calls": 0,
                "output": args.output.as_posix(),
            }
        )
    )


if __name__ == "__main__":
    main()

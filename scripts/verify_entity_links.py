"""Replay saved official JSON evidence; no network, database, or model calls."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from entities.links import LinkRequest, assess_links, save_assessment
from entities.scopes import RecordLocator, build_scope, load_scope_rules
from ingestion.records import content_hash
from ingestion.snapshots import SnapshotStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument(
        "--index", type=Path, default=Path("docs/data/scope-source-observations-2026-10-26.json")
    )
    parser.add_argument(
        "--reference", type=Path, default=Path("evals/entity-link-reference-v0.1.json")
    )
    parser.add_argument("--artifacts", type=Path, default=Path(".local/entity-link-verification"))
    parser.add_argument("--output", type=Path, default=Path(".local/entity-link-real-checks.json"))
    args = parser.parse_args()
    rules = load_scope_rules()
    store = SnapshotStore(args.root)
    index = json.loads(args.index.read_bytes())
    references = json.loads(args.reference.read_bytes())
    checks = []
    fact_checks = []
    for entry in index["sources"]:
        snapshot = store.verify(entry["snapshot_id"])
        assert snapshot.raw and snapshot.raw.sha256 == entry["raw_sha256"]
        assert snapshot.fetched_at.isoformat() == entry["fetched_at"]
        scope = build_scope(
            args.root,
            RecordLocator(snapshot_id=entry["snapshot_id"], ordinal=entry["reviewed_ordinal"]),
            rules,
        )
        assert scope["source"]["source_version_id"] == entry["reviewed_version_id"]
        # Independently reopen original JSON and replay each excerpt's character offsets/hash.
        rows = json.loads(store.raw_bytes(snapshot.raw))["results"]
        raw_row = rows[entry["reviewed_ordinal"]]
        for item in scope["facts"]:
            evidence = item["evidence"]
            field = raw_row[evidence["field"]]
            assert field[evidence["start"] : evidence["end"]] == evidence["quote"]
            assert content_hash(field) == evidence["field_sha256"]
            fact_checks.append(
                {
                    "source_version_id": entry["reviewed_version_id"],
                    "facet": item["facet"],
                    "key": item["key"],
                    "canonical": item["value"],
                    "field_pointer": evidence["field_pointer"],
                    "quote_replayed": True,
                }
            )
    for case in references["cases"]:
        request = LinkRequest.model_validate({"left": case["left"], "right": case["right"]})
        raw_rows = {}
        for side in ("left", "right"):
            loc = getattr(request, side)
            snap = store.verify(loc.snapshot_id)
            raw_rows[side] = json.loads(store.raw_bytes(snap.raw))["results"][loc.ordinal]
            for excerpt in case["must_contain"][side]:
                assert any(excerpt in str(value) for value in raw_rows[side].values())
        for field in case["same_raw_fields"]:
            assert raw_rows["left"][field] == raw_rows["right"][field]
        result = assess_links(args.root, request, rules)
        observed = {link["relation_type"]: link["confirmation_status"] for link in result["links"]}
        assert observed == case["expected"]
        assert all(link["causation"] == "not_established" for link in result["links"])
        artifact = save_assessment(args.artifacts, result)
        assert save_assessment(args.artifacts, result) == artifact
        checks.append(
            {
                "case_id": case["case_id"],
                "observed": observed,
                "assessment_id": result["assessment_id"],
                "idempotent_save": True,
            }
        )
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "saved_real_official_json_and_agent_reviewed_reference_checks",
        "source_snapshots": len(index["sources"]),
        "source_response_rows": sum(s["record_count"] for s in index["sources"]),
        "scope_rules_version": rules.rules_version,
        "scope_rules_sha256": rules.sha256,
        "fact_checks": fact_checks,
        "cases": checks,
        "reference_labels": "implementation_agent_review_not_independent_human_annotation",
        "dataset_release": None,
        "historical_reconstruction": False,
        "external_model_calls": 0,
        "network_calls_during_replay": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "facts": len(fact_checks),
                "cases": len(checks),
                "output": args.output.as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

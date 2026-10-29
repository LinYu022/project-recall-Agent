"""Check literal scope/negative-year/reference integrity without asking any model."""

import argparse
from collections import Counter
from pathlib import Path

from entities.normalize import EntityRules, project_record
from ingestion.records import content_hash, prepare_import
from parsing.citations import checked_parse
from parsing.parser import text_hash
from releases.bundle import BundleStore
from retrieval.contracts import SearchScope
from retrieval.index import checked_index
from retrieval.scope import classify_scope
from reviewing.corpus import read, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = read(args.candidate)
    store = BundleStore(Path(value["release_root"]))
    manifest = store.manifests.read_json(value["release"]["manifest_sha256"])
    base = store.directory(manifest)
    index = checked_index(base / "index", manifest["index_artifact_sha256"])
    corpus = index["corpus"]
    rules = EntityRules.model_validate(read(base / "rules/entity.json"))
    batches = [prepare_import(base / "raw", r["snapshot_id"]) for r in value["sources"]]
    literal = []
    for batch in batches:
        for row in batch.rows:
            assert row.version
            projection = project_record(row.version, rules)
            vehicle = projection["vehicles"][0]
            assert vehicle["make"]["canonical"] == row.version["payload"]["Make"].strip().upper()
            assert vehicle["model"]["canonical"] == row.version["payload"]["Model"].strip().upper()
            literal.append(row.version["version_id"])
    parsed = {}
    for digest in {d["parse_artifact_sha256"] for d in corpus["documents"]}:
        document = checked_parse(base / "raw", base / "parsed", digest)
        for unit in document["units"]:
            parsed[unit["unit_id"]] = (document, unit)
    checks = []
    for case in value["cases"]:
        scope = SearchScope.model_validate(case["scope"])
        states = []
        for doc in corpus["documents"]:
            if doc["metadata"]["source_id"] in case["sources"]:
                states.append(classify_scope(doc["metadata"], scope)[0])
        counts = dict(Counter(states))
        if case["category"] == "scope_exclusion":
            assert counts.get("matched", 0) == 0 and counts.get("pending", 0) == 0
        else:
            assert counts.get("matched", 0) > 0
        ref = value["references"][case["case_id"]]
        assert content_hash(ref) == case["reference_sha256"]
        for slot in ref["slots"]:
            for alt in slot["alternatives"]:
                document, unit = parsed[alt["unit_id"]]
                assert document["source"]["source_version_id"] == alt["source_version_id"]
                assert text_hash(unit["text"][alt["start"] : alt["end"]]) == alt["quote_sha256"]
        checks.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "scope_document_counts": counts,
                "reference_integrity": True,
            }
        )
    result = {
        "schema_version": "quality-confirmation-preflight/0.1",
        "candidate_sha256": content_hash(value),
        "release_id": value["release"]["release_id"],
        "literal_source_rows_checked": len(literal),
        "checks": checks,
        "new_provider_calls": 0,
        "new_source_requests": 0,
        "independent_reference_semantic_review": False,
        "quality_targets_frozen": False,
        "formal_model_quality_acceptance": "not_established",
    }
    write_new(args.output, result)
    print(
        {
            "cases": len(checks),
            "literal_rows": len(literal),
            "provider_calls": 0,
            "output": args.output.as_posix(),
        }
    )


if __name__ == "__main__":
    main()

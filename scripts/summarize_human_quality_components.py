"""Import only actually supplied human spans/merges, preserving partial and context roles."""

import argparse
from collections import Counter
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def bind_atoms(source, packet):
    row = next(r for r in packet["items"] if r["case_id"] == source["case_id"])
    claims = {c["claim"]["claim_id"]: c for c in row["claims"]}
    navigation = {a["atom_id"]: a for c in row["claims"] for a in c["candidate_units"]}
    adopted = []
    used: set[str] = set()
    for item in source["adopted_atoms"]:
        pieces = [navigation[key] for key in item["navigation_ids"]]
        if used & set(item["navigation_ids"]):
            raise ValueError("human_atom_repeats_navigation")
        used.update(item["navigation_ids"])
        if not all(a["end"] == b["start"] for a, b in zip(pieces, pieces[1:])):
            raise ValueError("human_merge_not_contiguous")
        claim_id = item["atom_id"].split("-N")[0]
        if any(not key.startswith(claim_id + "-A") for key in item["navigation_ids"]):
            raise ValueError("human_merge_crosses_claim_boundary")
        claim = claims[claim_id]["claim"]
        if not set(item["support_ids"]) <= set(claim["support_ids"]):
            raise ValueError("human_component_adds_uncited_evidence")
        quote = "".join(a["quote"] for a in pieces)
        start, end = pieces[0]["start"], pieces[-1]["end"]
        if claim["statement"][start:end] != quote:
            raise ValueError("human_component_span_changed")
        adopted.append(item | {"claim_id": claim_id, "start": start, "end": end, "quote": quote})
    if source["assertion_coverage_exhaustive"] and used != set(navigation):
        raise ValueError("human_complete_atom_coverage_missing")
    if set(source["original_navigation_judgments"]) != set(navigation):
        raise ValueError("original_human_navigation_judgment_missing")
    atom_ids = {a["atom_id"] for a in adopted}
    if any(not set(a.get("context_atoms", [])) <= atom_ids for a in adopted):
        raise ValueError("human_component_uses_unknown_context_atom")
    return {
        "case_id": row["case_id"],
        "source_packet_sha256": row["packet_sha256"],
        "human_source_sha256": content_hash(source),
        "atoms": adopted,
        "atom_counts": dict(Counter(a["verdict"] for a in adopted)),
        "obligations": source["obligations"],
        "all_requirements": source["all_requirements_verdict"],
        "exhaustive": source["assertion_coverage_exhaustive"],
        "original_candidate_judgments": source["original_navigation_judgments"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = read(args.packet)
    sources = sorted(Path("docs/experiments").glob("P1-16-quality-human-*-components.json"))
    rows = []
    for path in sources:
        source = read(path)
        if source["source_kind"] != "actual_human_reply" or source["reviewer_kind"] != "human":
            raise ValueError("not_actual_human_source")
        rows.append(bind_atoms(source, packet) | {"source_path": path.as_posix()})
    seen = {r["case_id"] for r in rows}
    planned = {r["case_id"] for r in packet["items"]}
    totals = Counter(a["verdict"] for r in rows for a in r["atoms"])
    result = {
        "schema_version": "actual-human-development-components/0.1",
        "planned_generated_tasks": len(planned),
        "reviewed_tasks": len(rows),
        "missing_tasks": sorted(planned - seen),
        "rows": rows,
        "atomic_counts": dict(totals),
        "observed_unsupported_fraction": totals["unsupported"] / sum(totals.values())
        if totals
        else None,
        "complete_development_component_baseline": seen == planned,
        "independent_reviewers": 1,
        "independent_seen_event_clusters": 2,
        "confidence_interval": None,
        "population_quality": None,
        "new_provider_calls": 0,
        "notes": "Partial/unknown navigation fragments merged only at the human's explicit request; no model verdict promoted to human.",
    }
    write_new(args.output, result)
    print(
        {
            "reviewed": len(rows),
            "missing": result["missing_tasks"],
            "atomic_counts": dict(totals),
            "output": args.output.as_posix(),
        }
    )


if __name__ == "__main__":
    main()

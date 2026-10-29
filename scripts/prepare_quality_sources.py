"""Capture held-out candidate campaign receipts without running or tuning a generator."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ingestion.contracts import SourceSpec
from ingestion.records import content_hash
from ingestion.snapshots import SnapshotStore
from reviewing.corpus import read, write_new

GROUPS = {
    "Q-HYUNDAI-LFB": ["25V065000", "25V235000"],
    "Q-TESLA-Y": ["24V935000", "25V002000"],
    "Q-TACOMA": ["25V058000", "25V656000"],
    "Q-HONDA-STEERING": ["24V744000"],
    "Q-BMW-BRAKE": ["24V104000", "24V697000"],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    codes = [c for group in GROUPS.values() for c in group]
    old = read("evals/comprehensive-v0.1/protocol.json")
    prior = set(old["previously_seen_campaigns"]) | {
        c for group in old["cluster_campaigns"].values() for c in group
    }
    if set(codes) & prior:
        raise ValueError("candidate_leaks_prior_campaigns")
    selection = {
        "groups": GROUPS,
        "screening": ".local/q16/source-screening-v2/screening.json",
        "sampling_basis": "predeclared brands plus available contrasting or related campaigns; not model outputs",
        "maximum_requests": len(codes),
        "generator_changed": False,
        "not_frozen_test_yet": True,
        "no_historical_visibility_claim": True,
    }
    write_new(args.root / "selection.json", selection)
    if not args.capture:
        print({"selection": (args.root / "selection.json").as_posix(), "requests": 0})
        return
    reservation = args.root / "reserved.json"
    if reservation.exists():
        raise ValueError("already_reserved_no_automatic_redownload")
    write_new(reservation, {"selection_sha256": content_hash(selection), "campaigns": codes})
    store = SnapshotStore(args.root / "raw")

    def capture(code):
        spec = SourceSpec(
            source_id="NHTSA-QA-CAMPAIGN-" + code,
            kind="recall_api",
            url="https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=" + code,
            selection={"campaign_number": code},
            usage="evaluation_only",
        )
        snap = store.capture(spec)
        return snap.model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(capture, codes))
    write_new(
        args.root / "receipts.json",
        {
            "selection_sha256": content_hash(selection),
            "snapshots": receipts,
            "new_source_requests": len(codes),
            "new_model_requests": 0,
        },
    )
    print(
        {
            "receipts": (args.root / "receipts.json").as_posix(),
            "states": [
                (r["source"]["source_id"], r["status"], r["record_count"]) for r in receipts
            ],
        }
    )


if __name__ == "__main__":
    main()

"""Restore exact observed official bytes; changed remote content cannot replace an old release."""

import argparse
import json
from pathlib import Path

from ingestion.contracts import Snapshot
from ingestion.snapshots import SnapshotStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--receipts", type=Path, default=Path("evals/comprehensive-v0.1/receipts.json")
    )
    args = parser.parse_args()
    store = SnapshotStore(Path(".local/source-snapshots"))
    values = json.loads(args.receipts.read_bytes())["snapshots"]
    for value in values:
        original = Snapshot.model_validate(value)
        if original.raw is None or original.status != "captured":
            raise ValueError("cannot_restore_an_unavailable_archived_selection")
        path = store.root / "snapshots" / (original.snapshot_id + ".json")
        if path.exists():
            if store.verify(original.snapshot_id) != original:
                raise ValueError("archived_evaluation_receipt_changed")
            continue
        try:
            store.raw_bytes(original.raw)
        except FileNotFoundError:
            if not args.download:
                raise ValueError("missing_saved_raw_bytes_explicit_download_required") from None
            fresh = store.capture(original.source)
            if fresh.status != "captured" or fresh.raw != original.raw:
                raise ValueError("remote_official_content_differs_from_frozen_receipt")
        # This is an archived observed receipt, not a claim that the new download
        # happened at the old time. A fresh capture retains its separate actual time.
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(original.model_dump_json(indent=2) + "\n")
        store.verify(original.snapshot_id)
    acquisition = Path(".local/P1-16/acquisition.json")
    if not acquisition.exists():
        acquisition.parent.mkdir(parents=True, exist_ok=True)
        with acquisition.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "classification": "restored_archived_receipts_with_separate_fresh_download_receipts",
                    "selection_sha256": json.loads(args.receipts.read_bytes())["selection_sha256"],
                    "snapshots": values,
                    "new_source_requests": 0,
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )
    print(json.dumps({"restored_exact_sources": len(values), "generation_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Restore recorded receipts only when newly fetched bytes match their original hashes."""

import argparse
import json
from pathlib import Path

from ingestion.contracts import Snapshot
from ingestion.snapshots import SnapshotStore, fetch_http
from ingestion.sources import load_sources


def restore_sources(root: Path, receipts: list[dict], fetch=fetch_http) -> dict:
    store, registry = SnapshotStore(root), load_sources()
    restored, verified, observations = [], [], []
    for value in receipts:
        original = Snapshot.model_validate(value)
        assert original.raw is not None and original.status == "captured"
        known = registry[original.source.source_id]
        if known.url != original.source.url or known.kind != original.source.kind:
            raise ValueError("receipt source is outside the fixed official registry")
        target = root / "snapshots" / (original.snapshot_id + ".json")
        if target.exists():
            if store.verify(original.snapshot_id) != original:
                raise ValueError("existing receipt differs; no overwrite")
            verified.append(original.snapshot_id)
            continue
        # The new observation is saved separately with today's actual fetch time.
        current = store.capture(original.source, fetch)
        observations.append(current.snapshot_id)
        if current.status != "captured" or current.raw != original.raw:
            raise ValueError("current bytes differ from recorded source; exact replay unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(original.model_dump_json(indent=2) + "\n")
        store.verify(original.snapshot_id)
        restored.append(original.snapshot_id)
    return {
        "restored_recorded_receipts": restored,
        "already_verified_receipts": verified,
        "new_actual_fetch_observations": observations,
        "historical_basis": "imported_project_receipts_not_independent_early_archive",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument(
        "--receipts", type=Path, default=Path("docs/data/retrieval-snapshot-receipts-v0.1.json")
    )
    args = parser.parse_args()
    report = restore_sources(args.root, json.loads(args.receipts.read_bytes())["snapshots"])
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

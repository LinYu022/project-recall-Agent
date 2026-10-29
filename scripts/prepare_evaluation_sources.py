"""Capture preselected official evaluation sources once, retaining unavailable selections."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ingestion.contracts import SourceSpec
from ingestion.records import content_hash
from ingestion.snapshots import SnapshotStore
from runtime.artifacts import ArtifactStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true", help="Explicit official HTTP acquisition")
    parser.add_argument("--output", type=Path, default=Path(".local/P1-16/acquisition.json"))
    args = parser.parse_args()
    selection = json.loads(Path("examples/evaluation-sources.json").read_bytes())
    store = SnapshotStore(Path(".local/source-snapshots"))
    if args.output.exists():
        previous = json.loads(args.output.read_bytes())
        if previous["selection_sha256"] != content_hash(selection):
            raise ValueError("acquisition_selection_changed")
        for snapshot in previous["snapshots"]:
            assert store.verify(snapshot["snapshot_id"]).model_dump(mode="json") == snapshot
        print(json.dumps({"reused_receipts": len(previous["snapshots"]), "new_requests": 0}))
        return 0
    if not args.capture:
        parser.error("No saved acquisition; --capture is required for official HTTP requests")
    if len(selection["campaigns"]) != selection["maximum_requests"]:
        raise ValueError("source_count_differs_from_bound")
    prior = []
    for path in sorted((store.root / "snapshots").glob("*.json")):
        snapshot = json.loads(path.read_bytes())
        code = snapshot["source"]["selection"].get("campaign_number")
        if code in selection["campaigns"]:
            raise ValueError("evaluation_source_already_observed:" + code)
        prior.append(
            {"snapshot_id": snapshot["snapshot_id"], "source_id": snapshot["source"]["source_id"]}
        )
    # Reserve each selected source before HTTP. A crash leaves an explicit unknown receipt;
    # it is never guessed away and automatically resent on a later invocation.
    reservation = args.output.with_suffix(".reserved.json")
    reservation.parent.mkdir(parents=True, exist_ok=True)
    with reservation.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "selection_sha256": content_hash(selection),
                "prior_observations": prior,
                "reserved_campaigns": selection["campaigns"],
            },
            stream,
            indent=2,
        )

    def capture(code):
        spec = SourceSpec(
            source_id="NHTSA-EVAL-CAMPAIGN-" + code,
            kind="recall_api",
            url="https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=" + code,
            selection={"campaign_number": code},
            max_bytes=selection["per_source_maximum_bytes"],
            usage="evaluation_only",
        )
        receipt = store.capture(spec)
        print(
            json.dumps(
                {"campaign": code, "status": receipt.status, "records": receipt.record_count}
            ),
            flush=True,
        )
        return receipt.model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(capture, selection["campaigns"]))
    value = {
        "classification": "official_HTTP_acquisition_no_generation_no_model_tuning",
        "selection_sha256": content_hash(selection),
        "snapshots": snapshots,
        "prior_observations_sha256": content_hash(prior),
        "new_source_requests": len(snapshots),
    }
    value["artifact"] = ArtifactStore(Path(".local/P1-16/acquisition")).put_json(value)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": args.output.as_posix(), "requests": len(snapshots)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

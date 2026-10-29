"""Public NHTSA screening receipts; never generation inputs or retrospective tests."""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from ingestion.records import content_hash
from ingestion.snapshots import NoRedirects
from reviewing.corpus import write_new
from runtime.artifacts import ArtifactStore

VEHICLES = [
    ("HYUNDAI", "IONIQ 5", 2025),
    ("TESLA", "MODEL Y", 2025),
    ("TOYOTA", "TACOMA", 2025),
    ("HONDA", "CR-V", 2025),
    ("BMW", "X5", 2025),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if (args.root / "screening.json").exists():
        raise ValueError("screening_already_recorded_no_automatic_redownload")
    store = ArtifactStore(args.root / "raw")

    def fetch(vehicle):
        make, model, year = vehicle
        url = "https://api.nhtsa.gov/recalls/recallsByVehicle?" + urlencode(
            {"make": make, "model": model, "modelYear": year}
        )
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "recall-evidence-agent/quality-screening",
            },
        )
        started = datetime.now(UTC).isoformat()
        try:
            with build_opener(NoRedirects()).open(request, timeout=30) as response:
                raw = response.read(2000001)
                if len(raw) > 2000000:
                    raise ValueError("screening_payload_too_large")
                data = json.loads(raw)
            ref = store.put_json(
                {"raw_utf8": raw.decode("utf-8"), "raw_sha256": hashlib.sha256(raw).hexdigest()}
            )
            rows = data["results"]
            return {
                "vehicle": vehicle,
                "url": url,
                "started": started,
                "status": "captured",
                "raw": ref,
                "campaigns": [
                    {
                        "campaign": r["NHTSACampaignNumber"],
                        "component": r.get("Component"),
                        "manufacturer": r.get("Manufacturer"),
                        "summary": r.get("Summary"),
                    }
                    for r in rows
                ],
            }
        except Exception as error:
            return {
                "vehicle": vehicle,
                "url": url,
                "started": started,
                "status": "unavailable",
                "error_type": type(error).__name__,
            }

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(fetch, VEHICLES))
    value = {
        "classification": "official_readonly_source_screening_no_model_calls",
        "generator_tuning_from_results": False,
        "new_source_requests": len(VEHICLES),
        "receipts": receipts,
    }
    write_new(args.root / "screening.json", value)
    print(
        json.dumps(
            {
                "output": (args.root / "screening.json").as_posix(),
                "content_sha256": content_hash(value),
                "sources": [
                    {
                        "vehicle": r["vehicle"],
                        "status": r["status"],
                        "campaigns": [c["campaign"] for c in r.get("campaigns", [])],
                    }
                    for r in receipts
                ],
            }
        )
    )


if __name__ == "__main__":
    main()

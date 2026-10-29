"""Run the deterministic declaration extractor separately from task/model workers."""

import argparse
import json
from pathlib import Path
from threading import Event

import sqlalchemy as sa

from database.config import database_url
from memory.service import MemoryService
from releases.bundle import BundleStore
from releases.catalog import Catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    engine = sa.create_engine(
        database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
    )
    service = MemoryService(Catalog(engine, BundleStore(Path(".local/releases"))))
    try:
        while True:
            result = service.extract_once()
            if result:
                print(
                    json.dumps(
                        {
                            "job_id": result["job_id"],
                            "state": result["state"],
                            "candidates": len(result["candidate_ids"]),
                        }
                    ),
                    flush=True,
                )
            if args.once:
                return 0
            if result is None:
                Event().wait(1)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

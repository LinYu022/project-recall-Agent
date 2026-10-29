"""Exercise the query API through a real loopback listener; never fetch sources or call models."""

import argparse
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx2
import uvicorn

from app.query_api import QuerySettings, create_query_app
from ingestion.snapshots import FetchResult, SnapshotStore
from ingestion.sources import load_sources


def time_input(ids, cutoff=None):
    return {
        "snapshot_ids": ids,
        "time": {
            "mode": "historical_as_of" if cutoff else "snapshot_analysis",
            "event_interval": None,
            "knowledge_cutoff": cutoff,
            "date_filter_basis": "Pinned loopback verification; saved receipt visibility only",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--saved-official",
        action="store_true",
        help="Requires existing official snapshots and a prepared RECALL_DATABASE_URL",
    )
    parser.add_argument("--root", type=Path, default=Path(".local/source-snapshots"))
    parser.add_argument("--output", type=Path, default=Path(".local/query-http.json"))
    args = parser.parse_args()
    if args.saved_official:
        if not os.environ.get("RECALL_DATABASE_URL"):
            raise ValueError("official count replay requires a prepared database")
        settings = QuerySettings.model_validate_json(
            Path("examples/query-readonly.json").read_bytes()
        )
        settings.snapshot_root = args.root
        app = create_query_app(settings, development_mode=True)
        count_request = json.loads(Path("examples/count-complaints.json").read_bytes())
        references = json.loads(Path("evals/entity-link-reference-v0.1.json").read_bytes())["cases"]
    else:
        root = Path(".local") / ("query-http-synthetic-" + uuid4().hex)
        spec = load_sources()["NHTSA-API-CAMPAIGN-10V039000"]
        rows = [
            dict(
                NHTSACampaignNumber="10V039000",
                Make="FORD",
                Model="F-150",
                ModelYear="2016",
                Component=("SERVICE BRAKES", "POWER TRAIN")[i],
                Summary="SYNTHETIC HTTP " + str(i),
                ReportReceivedDate="01/01/2020",
            )
            for i in range(2)
        ]
        payload = json.dumps({"Count": 2, "results": rows}).encode()
        snapshot = SnapshotStore(root).capture(spec, lambda _: FetchResult(200, {}, payload))
        settings = QuerySettings(snapshot_root=root, allowed_snapshot_ids=[snapshot.snapshot_id])
        # The synthetic smoke test intentionally has no database dependency.
        previous = os.environ.pop("RECALL_DATABASE_URL", None)
        try:
            app = create_query_app(settings, development_mode=True)
        finally:
            if previous is not None:
                os.environ["RECALL_DATABASE_URL"] = previous
        count_request = {
            "temporal": time_input([snapshot.snapshot_id]),
            "make": "FORD",
            "model": "F-150",
            "model_year": 2016,
            "components": [],
        }
        references = [
            {
                "case_id": "synthetic_scope",
                "left": {"snapshot_id": snapshot.snapshot_id, "ordinal": 0},
                "right": {"snapshot_id": snapshot.snapshot_id, "ordinal": 1},
            }
        ]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    checks = []
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "query loopback server did not start"
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False
        ) as client:

            def post(case, endpoint, body, expected_http=200):
                response = client.post(endpoint, json=body)
                assert response.status_code == expected_http, (case, response.status_code)
                value = response.json()
                checks.append(
                    {
                        "case": case,
                        "http_status": response.status_code,
                        "status": value.get("status"),
                        **{
                            k: value[k]
                            for k in (
                                "distinct_complaints",
                                "matching_component_rows",
                                "unknown_production_date_versions",
                                "reason",
                            )
                            if k in value
                        },
                    }
                )
                return value

            assert client.get("/health").json()["model_provider"] is None
            checks.append({"case": "health", "http_status": 200})
            endpoint = "/queries/complaints/count"
            if args.saved_official:
                ids = count_request["temporal"]["snapshot_ids"]
                raw = []
                store = SnapshotStore(settings.snapshot_root)
                for identifier in ids:
                    snap = store.verify(identifier)
                    raw.extend(json.loads(store.raw_bytes(snap.raw))["results"])
                variants = [
                    ("all_complaints", [], len({r["odiNumber"] for r in raw})),
                    (
                        "power_train_or_brakes",
                        ["POWER TRAIN", "SERVICE BRAKES"],
                        len(
                            {
                                r["odiNumber"]
                                for r in raw
                                if set(r["components"].split(","))
                                & {"POWER TRAIN", "SERVICE BRAKES"}
                            }
                        ),
                    ),
                    (
                        "brakes",
                        ["SERVICE BRAKES"],
                        len(
                            {
                                r["odiNumber"]
                                for r in raw
                                if "SERVICE BRAKES" in r["components"].split(",")
                            }
                        ),
                    ),
                ]
                for name, components, expected in variants:
                    result = post(name, endpoint, count_request | {"components": components})
                    assert (
                        result["status"] == "counted" and result["distinct_complaints"] == expected
                    )
                partial = post(
                    "unknown_production",
                    endpoint,
                    count_request
                    | {"production_interval": {"start": "2015-01-01", "end": "2017-12-31"}},
                )
                assert partial["status"] == "partial" and partial["distinct_complaints"] == 0
                assert partial["unknown_production_date_versions"] == variants[1][2]
                zero = post(
                    "zero_matching_labels",
                    endpoint,
                    count_request
                    | {"components": ["SERVICE BRAKES, AIR:ANTILOCK:CONTROL UNIT/MODULE"]},
                )
                assert zero["status"] == "counted" and zero["distinct_complaints"] == 0
            else:
                result = post("no_complaint_input", endpoint, count_request)
                assert result["status"] == "unsupported" and result["distinct_complaints"] is None
            historical = count_request | {
                "temporal": time_input(
                    count_request["temporal"]["snapshot_ids"], "2000-01-01T00:00:00Z"
                )
            }
            result = post("historical_count_unsupported", endpoint, historical)
            assert result["status"] == "unsupported" and result["distinct_complaints"] is None
            post("invalid_model", endpoint, count_request | {"model": "F150"}, 422)
            post(
                "unregistered_snapshot",
                endpoint,
                count_request | {"temporal": time_input(["0" * 32])},
                403,
            )
            for ref in references:
                ids = sorted({ref["left"]["snapshot_id"], ref["right"]["snapshot_id"]})
                body = {"temporal": time_input(ids), "left": ref["left"], "right": ref["right"]}
                result = post(ref["case_id"], "/queries/entity-links", body)
                assert result["status"] == "assessed"
                observed = {
                    link["relation_type"]: link["confirmation_status"]
                    for link in result["assessment"]["links"]
                }
                if "expected" in ref:
                    assert observed == ref["expected"]
                checks[-1]["relations"] = observed
                assert all(
                    link["causation"] == "not_established" for link in result["assessment"]["links"]
                )
            body["temporal"] = time_input(ids, "2000-01-01T00:00:00Z")
            result = post("historical_links_unsupported", "/queries/entity-links", body)
            assert result["status"] == "unsupported" and result["assessment"] is None
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive(), "query server did not stop"
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_loopback_http_saved_official_json_and_real_postgresql"
        if args.saved_official
        else "real_loopback_http_synthetic_json_without_database",
        "server_stopped": True,
        "host": "127.0.0.1",
        "port": port,
        "checks": checks,
        "external_source_requests": 0,
        "external_model_calls": 0,
        "independent_human_labels": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": len(checks),
                "server_stopped": True,
                "output": args.output.as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Start and verify loopback HTTP; run with python -m scripts.verify_http."""

import argparse
import hashlib
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/http-verification.json"))
    args = parser.parse_args()
    task = json.loads(Path("examples/demo-task.json").read_text(encoding="utf-8"))
    old_directory = os.environ.get("RECALL_ARTIFACT_DIR")
    os.environ["RECALL_ARTIFACT_DIR"] = ".local/http-smoke-runs"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config("app.api:create_app", factory=True, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    checks = []
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "local server failed to start"
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
        ) as client:
            health = client.get("/health")
            assert health.status_code == 200
            checks.append({"case": "health", "http_status": 200})
            scenarios = [
                ("success", {}, 201, None),
                ("empty", {"scenario": "empty"}, 201, None),
                ("denied", {"tool": "not_allowed"}, 403, "tool_not_allowed"),
                ("model-error", {"model_scenario": "model-error"}, 502, "model_error"),
                ("bad-citation", {"model_scenario": "bad-citation"}, 502, "model_citation_invalid"),
            ]
            for name, fields, status, error in scenarios:
                response = client.post("/demo/runs", json={"task": task, **fields})
                result = response.json()
                assert response.status_code == status, result
                assert result["error_code"] == error, result
                checks.append({"case": name, "http_status": status, "result": result})
                assert client.get(result["links"]["self"]).json() == result
                assert client.get(result["links"]["events"]).status_code == 200
                for field in (
                    "input_artifact",
                    "output_artifact",
                    "model_input_artifact",
                    "model_output_artifact",
                ):
                    if result.get(field):
                        artifact = client.get(result["links"][field])
                        assert artifact.status_code == 200
                        assert (
                            hashlib.sha256(artifact.content).hexdigest() == result[field]["sha256"]
                        )
            bad_task = json.loads(json.dumps(task))
            del bad_task["scope"]["make"]
            invalid = client.post("/demo/runs", json={"task": bad_task})
            assert invalid.status_code == 422
            checks.append({"case": "missing-make", "http_status": 422})
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=5)
        listener.close()
        if old_directory is None:
            os.environ.pop("RECALL_ARTIFACT_DIR", None)
        else:
            os.environ["RECALL_ARTIFACT_DIR"] = old_directory
    assert not thread.is_alive(), "local server did not stop"
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_loopback_http_with_synthetic_data_and_fake_model",
        "host": "127.0.0.1",
        "port": port,
        "server_stopped": True,
        "external_model_calls": 0,
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"server_stopped": True, "checks": len(checks), "output": args.output.as_posix()}
        )
    )


if __name__ == "__main__":
    main()

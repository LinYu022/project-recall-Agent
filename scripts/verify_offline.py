"""Local equivalent of the offline CI job, without model or source downloads."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree


def prepare_root(logs_root: Path | None) -> Path:
    root = (logs_root or Path(".local/offline-checks") / uuid4().hex).resolve()
    workspace = Path.cwd().resolve()
    if root == workspace or not root.is_relative_to(workspace):
        raise ValueError("verification_logs_must_be_a_new_directory_inside_workspace")
    # pytest can recursively clear basetemp; a fresh verified directory contains no prior work.
    root.mkdir(parents=True, exist_ok=False)
    assert (root / "pytest").resolve().is_relative_to(workspace)
    return root.relative_to(workspace)


def prepare_pytest_root() -> Path:
    # Qdrant and direct pathlib test fixtures may not support Windows extended paths.
    # Keep disposable pytest data short even when users choose descriptive log names.
    return prepare_root(Path(".local/pt") / uuid4().hex[:12])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/offline-verification.json"))
    parser.add_argument(
        "--logs-root", type=Path, help="New workspace directory; existing directories are refused"
    )
    args = parser.parse_args()
    root = prepare_root(args.logs_root)
    pytest_root = prepare_pytest_root()
    checks = []
    commands = [
        ["ruff", "format", "--check", "."],
        ["ruff", "check", "."],
        ["mypy"],
        ["mypy", "reviewing", "baselining"],
        ["alembic", "upgrade", "head", "--sql"],
        ["pytest", "-q", "--basetemp", str(pytest_root), "--junitxml", str(root / "tests.xml")],
        ["scripts.verify_http", "--output", str(root / "http.json")],
        ["scripts.verify_query_http", "--output", str(root / "query-http.json")],
    ]
    for arguments in commands:
        result = subprocess.run(
            [sys.executable, "-m", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        checks.append({"command": ["python", "-m", *arguments], "exit_code": result.returncode})
        (root / (arguments[0].replace(".", "-") + "-" + str(len(checks)) + ".log")).write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        print(json.dumps(checks[-1]), flush=True)
        if result.returncode:
            print(result.stdout + result.stderr)
            return result.returncode
    suite = ElementTree.parse(root / "tests.xml").getroot().find("testsuite")
    http = json.loads((root / "http.json").read_text(encoding="utf-8"))
    query_http = json.loads((root / "query-http.json").read_bytes())
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "local_equivalent_of_offline_ci_with_synthetic_data_and_fake_model",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "checks": checks,
        "tests": {
            key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")
        },
        "http_checks": len(http["checks"]),
        "http_server_stopped": http["server_stopped"],
        "query_http_checks": len(query_http["checks"]),
        "query_http_server_stopped": query_http["server_stopped"],
        "external_model_calls": 0,
        "github_actions_run": False,
        "fingerprints": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock", ".github/workflows/ci.yml")
        },
        "logs": root.as_posix(),
        "pytest_temp": pytest_root.as_posix(),
        "immutable_record": (root / "verification.json").as_posix(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    (root / "verification.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "passed", "output": args.output.as_posix()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

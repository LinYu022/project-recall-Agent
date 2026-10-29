"""Run real integration tests on an isolated portable PostgreSQL instance.

Usage: python -m scripts.verify_postgres --bin-dir PATH_TO_POSTGRES_BIN
The binaries must already be installed or downloaded from a trusted source.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import sqlalchemy as sa
from sqlalchemy.engine import URL


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".local/postgres-verification.json"))
    parser.add_argument("--archive", type=Path, help="Optional local binary archive to fingerprint")
    parser.add_argument(
        "--source-index",
        type=Path,
        help="Also verify imports of these saved official snapshot receipts",
    )
    parser.add_argument(
        "--entity-checks", action="store_true", help="Include entity checks with --source-index"
    )
    parser.add_argument("--query-checks", action="store_true", help="Include official query HTTP")
    parser.add_argument(
        "--release-checks",
        action="store_true",
        help="Publish/bind/query prepared official release bundles on this disposable instance",
    )
    parser.add_argument("--investigation-checks", action="store_true")
    parser.add_argument("--control-checks", action="store_true")
    parser.add_argument("--agent-checks", action="store_true")
    parser.add_argument("--dispatch-checks", action="store_true")
    parser.add_argument("--cooperation-checks", action="store_true")
    parser.add_argument("--memory-checks", action="store_true")
    parser.add_argument("--episode-checks", action="store_true")
    parser.add_argument("--procedure-checks", action="store_true")
    parser.add_argument("--evaluation-preview", action="store_true")
    parser.add_argument("--cooperation-method")
    parser.add_argument("--cooperation-case-id")
    parser.add_argument(
        "--test-filter", help="Select a named pytest subset for diagnosis; not a full verification"
    )
    parser.add_argument("--agent-model-call-cap", type=int, default=6)
    parser.add_argument("--agent-profile-record", type=Path)
    parser.add_argument("--reuse-recorded", action="store_true")
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Explicitly enable at most four external generation requests",
    )
    parser.add_argument(
        "--replay-model",
        action="store_true",
        help="Use previously recorded API responses, no generation network calls",
    )
    args = parser.parse_args()
    if args.entity_checks and not args.source_index:
        parser.error("--entity-checks requires --source-index")
    if args.query_checks and not args.entity_checks:
        parser.error("--query-checks requires --entity-checks")
    if (args.live_model or args.replay_model) and not (
        args.investigation_checks or args.agent_checks
    ):
        parser.error("model flags require --investigation-checks or --agent-checks")
    if args.reuse_recorded and not args.live_model:
        parser.error("--reuse-recorded requires --live-model")
    binaries = args.bin_dir.resolve()
    suffix = ".exe" if os.name == "nt" else ""
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run_program(name, *arguments, **kwargs):
        return subprocess.run(
            [str(binaries / (name + suffix)), *map(str, arguments)],
            check=True,
            creationflags=creationflags,
            **kwargs,
        )

    version = run_program("postgres", "--version", capture_output=True, text=True).stdout.strip()
    root = (Path(".local") / ("postgres-check-" + uuid4().hex)).resolve()
    root.mkdir(parents=True)
    data = root / "data"
    password_file = root / "password.txt"
    password = secrets.token_urlsafe(32)
    password_file.write_text(password + "\n", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    started = False
    engine = None
    test_result = None
    source_result = None
    release_result = None
    investigation_result = None
    control_result = None
    agent_result = None
    dispatch_result = None
    cooperation_result = None
    memory_result = None
    episode_result = None
    procedure_result = None
    evaluation_preview_result = None
    try:
        with (root / "initdb.log").open("w", encoding="utf-8") as log:
            run_program(
                "initdb",
                "-D",
                data,
                "-U",
                "recall_test",
                "--auth=scram-sha-256",
                "--pwfile",
                password_file,
                "--encoding=UTF8",
                "--locale=C",
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        password_file.unlink()
        run_program(
            "pg_ctl",
            "-D",
            data,
            "-l",
            root / "server.log",
            "-o",
            f"-h 127.0.0.1 -p {port}",
            "-w",
            "-t",
            "30",
            "start",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started = True
        url = URL.create(
            "postgresql+psycopg",
            username="recall_test",
            password=password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        )
        engine = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
        with engine.connect() as connection:
            server_version = connection.scalar(sa.text("SHOW server_version"))
        environment = os.environ | {
            "RECALL_TEST_DATABASE_URL": url.render_as_string(hide_password=False)
        }
        test_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                "postgres",
                "--basetemp",
                str(root / "pytest"),
                "--junitxml",
                str(root / "tests.xml"),
                *(["-k", args.test_filter] if args.test_filter else []),
            ],
            env=environment,
            creationflags=creationflags,
            check=False,
        )
        if test_result.returncode == 0 and args.source_index:
            source_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_source_import",
                    "--index",
                    str(args.source_index),
                    "--output",
                    str(root / "source-import.json"),
                    *(["--entity-checks"] if args.entity_checks else []),
                    *(["--query-checks"] if args.query_checks else []),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.release_checks:
            release_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_releases_real",
                    "--output",
                    str(root / "release-real.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.investigation_checks:
            investigation_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_investigation_real",
                    "--output",
                    str(root / "investigation-real.json"),
                    *(["--live-model"] if args.live_model else []),
                    *(["--replay-model"] if args.replay_model else []),
                    *(["--reuse-recorded"] if args.reuse_recorded else []),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.control_checks:
            control_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_control_real",
                    "--output",
                    str(root / "control-real.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.agent_checks:
            agent_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_single_agent_real",
                    "--model-call-cap",
                    str(args.agent_model_call_cap),
                    "--output",
                    str(root / "single-agent.json"),
                    *(["--live-model"] if args.live_model else []),
                    *(["--replay-model"] if args.replay_model else []),
                    *(["--reuse-recorded"] if args.reuse_recorded else []),
                    *(
                        ["--profile-record", str(args.agent_profile_record)]
                        if args.agent_profile_record
                        else []
                    ),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.dispatch_checks:
            dispatch_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_dispatch_real",
                    "--output",
                    str(root / "dispatch-real.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.cooperation_checks:
            cooperation_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_cooperation_real",
                    "--output",
                    str(root / "cooperation-real.json"),
                    *(["--method", args.cooperation_method] if args.cooperation_method else []),
                    *(["--case-id", args.cooperation_case_id] if args.cooperation_case_id else []),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.memory_checks:
            memory_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_memory_real",
                    "--output",
                    str(root / "memory-real.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.episode_checks:
            episode_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_episodes_real",
                    "--output",
                    str(root / "episodes-real.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.procedure_checks:
            procedure_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_procedures_real",
                    "--output",
                    str(root / "procedures-real.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
        if test_result.returncode == 0 and args.evaluation_preview:
            evaluation_preview_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.verify_evaluation_preview",
                    "--output",
                    str(root / "evaluation-preview.json"),
                ],
                env=environment,
                creationflags=creationflags,
                check=False,
            )
    finally:
        if engine is not None:
            engine.dispose()
        if password_file.exists():
            password_file.unlink()
        # Try stopping even if startup timed out after the server was launched.
        if started or (data / "postmaster.pid").exists():
            run_program(
                "pg_ctl",
                "-D",
                data,
                "-m",
                "fast",
                "-w",
                "-t",
                "30",
                "stop",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    assert not (data / "postmaster.pid").exists(), "temporary server did not stop"
    suite = ElementTree.parse(root / "tests.xml").getroot().find("testsuite")
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "classification": "real_postgresql_with_synthetic_run_data",
        "binary_version": version,
        "server_version": server_version,
        "authentication": "ephemeral_password_scram_sha_256",
        "host": "127.0.0.1",
        "server_stopped": True,
        "external_model_calls": 0,
        "pytest_exit_code": test_result.returncode,
        "tests": {
            key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")
        },
        "cases": [case.attrib["name"] for case in suite.findall("testcase")],
        "test_filter": args.test_filter,
        "artifacts": root.relative_to(Path.cwd()).as_posix(),
    }
    if args.archive:
        with args.archive.open("rb") as archive:
            archive_hash = hashlib.file_digest(archive, "sha256").hexdigest()
        record["binary_archive"] = {
            "file": args.archive.name,
            "bytes": args.archive.stat().st_size,
            "sha256": archive_hash,
        }
    if source_result is not None:
        record["classification"] = (
            "real_postgresql_controlled_tests_plus_saved_official_snapshot_imports"
        )
        record["source_import_exit_code"] = source_result.returncode
        if (root / "source-import.json").exists():
            record["source_import"] = json.loads(
                (root / "source-import.json").read_text(encoding="utf-8")
            )
    if release_result is not None:
        record["release_check_exit_code"] = release_result.returncode
        record["release_real"] = (
            json.loads((root / "release-real.json").read_bytes())
            if (root / "release-real.json").exists()
            else None
        )
    if investigation_result is not None:
        record["investigation_check_exit_code"] = investigation_result.returncode
        record["investigation_real"] = (
            json.loads((root / "investigation-real.json").read_bytes())
            if (root / "investigation-real.json").exists()
            else None
        )
        record["external_model_calls"] = (record["investigation_real"] or {}).get(
            "external_generative_model_calls", 0
        )
    if control_result is not None:
        record["control_check_exit_code"] = control_result.returncode
        record["control_real"] = (
            json.loads((root / "control-real.json").read_bytes())
            if (root / "control-real.json").exists()
            else None
        )
    if agent_result is not None:
        record["agent_check_exit_code"] = agent_result.returncode
        record["single_agent"] = (
            json.loads((root / "single-agent.json").read_bytes())
            if (root / "single-agent.json").exists()
            else None
        )
        record["external_model_calls"] += (record["single_agent"] or {}).get(
            "external_model_calls", 0
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if cooperation_result is not None:
        record["cooperation_check_exit_code"] = cooperation_result.returncode
        record["cooperation_real"] = (
            json.loads((root / "cooperation-real.json").read_bytes())
            if (root / "cooperation-real.json").exists()
            else None
        )
    if dispatch_result is not None:
        record["dispatch_check_exit_code"] = dispatch_result.returncode
        record["dispatch_real"] = (
            json.loads((root / "dispatch-real.json").read_bytes())
            if (root / "dispatch-real.json").exists()
            else None
        )
    if memory_result is not None:
        record["memory_check_exit_code"] = memory_result.returncode
        record["memory_real"] = (
            json.loads((root / "memory-real.json").read_bytes())
            if (root / "memory-real.json").exists()
            else None
        )
    if episode_result is not None:
        record["episode_check_exit_code"] = episode_result.returncode
        record["episodes_real"] = (
            json.loads((root / "episodes-real.json").read_bytes())
            if (root / "episodes-real.json").exists()
            else None
        )
    if procedure_result is not None:
        record["procedure_check_exit_code"] = procedure_result.returncode
        record["procedures_real"] = (
            json.loads((root / "procedures-real.json").read_bytes())
            if (root / "procedures-real.json").exists()
            else None
        )
    if evaluation_preview_result is not None:
        record["evaluation_preview_exit_code"] = evaluation_preview_result.returncode
        record["evaluation_preview"] = (
            json.loads((root / "evaluation-preview.json").read_bytes())
            if (root / "evaluation-preview.json").exists()
            else None
        )
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"server_stopped": True, "tests": record["tests"], "output": args.output.as_posix()}
        )
    )
    return (
        test_result.returncode
        or (source_result.returncode if source_result else 0)
        or (release_result.returncode if release_result else 0)
        or (investigation_result.returncode if investigation_result else 0)
        or (control_result.returncode if control_result else 0)
        or (agent_result.returncode if agent_result else 0)
        or (dispatch_result.returncode if dispatch_result else 0)
        or (cooperation_result.returncode if cooperation_result else 0)
        or (memory_result.returncode if memory_result else 0)
        or (episode_result.returncode if episode_result else 0)
        or (procedure_result.returncode if procedure_result else 0)
        or (evaluation_preview_result.returncode if evaluation_preview_result else 0)
    )


if __name__ == "__main__":
    raise SystemExit(main())

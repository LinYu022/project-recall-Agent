"""Run an explicitly selected frozen evaluation on a persistent private local PostgreSQL."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import URL

from evaluation.freeze import load
from evaluation.records import atomic_json, exclusive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    parser.add_argument("--approved-experiment")
    parser.add_argument("--arms", nargs="*")
    parser.add_argument("--maximum-cells", type=int)
    parser.add_argument("--label-root", type=Path, default=Path("evals/comprehensive-v0.1"))
    args = parser.parse_args()
    identity = json.loads(args.experiment.read_bytes())
    load(identity)
    if args.mode == "live" and args.approved_experiment != identity["experiment_id"]:
        parser.error("Exact approved experiment ID is required before enabling provider requests")
    binaries = args.bin_dir.resolve()
    root = Path(".local/e16-db") / identity["experiment_id"][:12] / args.mode[0]
    root.mkdir(parents=True, exist_ok=True)
    data = (root / "data").resolve()
    suffix, flags = (".exe", subprocess.CREATE_NO_WINDOW) if os.name == "nt" else ("", 0)

    def program(name, *arguments, **options):
        return subprocess.run(
            [str(binaries / (name + suffix)), *map(str, arguments)],
            creationflags=flags,
            check=True,
            **options,
        )

    status = {
        "experiment_id": identity["experiment_id"],
        "mode": args.mode,
        "server_stopped": False,
    }
    with exclusive(root / "owner.json"):
        settings_file = root / "private-settings.json"
        if settings_file.exists():
            settings = json.loads(settings_file.read_bytes())
        else:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            settings = {"port": port, "password": secrets.token_urlsafe(32)}
            atomic_json(settings_file, settings)
        if (data / "postmaster.pid").exists():
            raise ValueError("evaluation_database_already_running_inspect_before_reuse")
        started = False
        try:
            if not (data / "PG_VERSION").exists():
                password = root / "initial-password.txt"
                password.write_text(settings["password"] + "\n", encoding="utf-8")
                try:
                    with (root / "initdb.log").open("w", encoding="utf-8") as log:
                        program(
                            "initdb",
                            "-D",
                            data,
                            "-U",
                            "recall_eval",
                            "--auth=scram-sha-256",
                            "--pwfile",
                            password.resolve(),
                            "--encoding=UTF8",
                            "--locale=C",
                            stdout=log,
                            stderr=subprocess.STDOUT,
                        )
                finally:
                    password.unlink(missing_ok=True)
            program(
                "pg_ctl",
                "-D",
                data,
                "-l",
                (root / "server.log").resolve(),
                "-o",
                f"-h 127.0.0.1 -p {settings['port']}",
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
                username="recall_eval",
                password=settings["password"],
                host="127.0.0.1",
                port=settings["port"],
                database="postgres",
            )
            arguments = [
                sys.executable,
                "-m",
                "evaluation",
                "run",
                "--experiment",
                str(args.experiment),
                "--mode",
                args.mode,
                "--label-root",
                str(args.label_root),
            ]
            if args.approved_experiment:
                arguments += ["--approved-experiment", args.approved_experiment]
            if args.arms:
                arguments += ["--arms", *args.arms]
            if args.maximum_cells:
                arguments += ["--maximum-cells", str(args.maximum_cells)]
            result = subprocess.run(
                arguments,
                env=os.environ
                | {"RECALL_EVAL_DATABASE_URL": url.render_as_string(hide_password=False)},
                creationflags=flags,
                check=False,
            )
            status["exit_code"] = result.returncode
        finally:
            if started or (data / "postmaster.pid").exists():
                program(
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
            status["server_stopped"] = True
            status["database_retained_for_explicit_resume"] = True
            atomic_json(root / "status.json", status)
    print(json.dumps({k: v for k, v in status.items() if k != "password"}))
    return status["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())

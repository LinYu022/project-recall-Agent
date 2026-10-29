"""An isolated local database lifecycle, used only when explicitly requested at startup."""

import os
import secrets
import socket
import subprocess
from contextlib import contextmanager
from typing import Any

from sqlalchemy.engine import URL


@contextmanager
def portable_postgres(bin_dir, root):
    root.mkdir(parents=True, exist_ok=False)
    data = root / "data"
    password = secrets.token_urlsafe(32)
    password_file = root / "password.txt"
    password_file.write_text(password, encoding="utf-8")
    options: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    )

    def run(name, *args, **kwargs):
        return subprocess.run(
            [str(bin_dir / (name + (".exe" if os.name == "nt" else ""))), *map(str, args)],
            check=True,
            **options,
            **kwargs,
        )

    started = False
    try:
        with (root / "initdb.log").open("w") as log:
            run(
                "initdb",
                "-D",
                data,
                "-U",
                "recall",
                "--auth=scram-sha-256",
                "--pwfile",
                password_file,
                "--encoding=UTF8",
                "--locale=C",
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        password_file.unlink()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        run(
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
        yield URL.create(
            "postgresql+psycopg",
            username="recall",
            password=password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        ).render_as_string(hide_password=False)
    finally:
        if password_file.exists():
            password_file.unlink()
        if started:
            run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop", stdout=subprocess.DEVNULL)

"""Identify the product layer independently from the historical experiment freeze."""

import hashlib
from pathlib import Path


def implementation():
    files = [*Path("portal").glob("*.py"), *Path("frontend/src").glob("*")]
    files += [
        Path(p)
        for p in (
            "frontend/package.json",
            "frontend/pnpm-lock.yaml",
            "frontend/index.html",
            "frontend/vite.config.js",
            "frontend/e2e.mjs",
            "Dockerfile",
            "compose.yaml",
        )
    ]
    files += [p for p in Path("frontend/dist").rglob("*") if p.is_file()]
    return {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}

"""Sealed protocol and code identity; never silently overwrite a freeze after seeing results."""

import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from evaluation.contracts import Protocol, cells, request_ceiling
from ingestion.records import content_hash
from runtime.artifacts import ArtifactStore


def implementation():
    paths = [
        p
        for directory in [
            "app",
            "control",
            "database",
            "domain",
            "entities",
            "evaluation",
            "ingestion",
            "investigation",
            "memory",
            "models",
            "parsing",
            "releases",
            "retrieval",
            "runtime",
        ]
        for p in Path(directory).rglob("*.py")
    ]
    paths += [
        Path("pyproject.toml"),
        Path("uv.lock"),
        Path("examples/procedure-event-families.json"),
    ]
    paths += list(Path("scripts").glob("*evaluation*.py"))
    return {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def seal(protocol, root, prefix):
    protocol = Protocol.model_validate(protocol)
    if content_hash(prefix) != protocol.frozen_prefix_sha256:
        raise ValueError("memory_prefix_differs_from_protocol")
    body = {
        "protocol": protocol.model_dump(mode="json"),
        "prefix": prefix,
        "implementation_sha256": implementation(),
        "cells": cells(protocol),
        "maximum_provider_requests": request_ceiling(protocol),
        "source_git_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip(),
    }
    experiment_id = content_hash(body)
    store = ArtifactStore(root / experiment_id)
    identity = {
        "experiment_id": experiment_id,
        "created_at": datetime.now(UTC).isoformat(),
        "git_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip(),
        "git_working_tree_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
            ).stdout.strip()
        ),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "artifact": store.put_json(body),
        "artifact_root": store.directory.parent.as_posix(),
    }
    pointer = root / experiment_id / "experiment.json"
    if pointer.exists():
        old = json.loads(pointer.read_bytes())
        load(old)
        return old
    with pointer.open("x", encoding="utf-8") as stream:
        json.dump(identity, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return identity


def load(identity, *, require_current=True):
    body = ArtifactStore(Path(identity["artifact_root"])).read_json(identity["artifact"]["sha256"])
    protocol = Protocol.model_validate(body["protocol"])
    if content_hash(body) != identity["experiment_id"] or body["cells"] != cells(protocol):
        raise ValueError("experiment_identity_changed")
    if require_current and body["implementation_sha256"] != implementation():
        raise ValueError("experiment_code_changed_requires_new_freeze")
    if content_hash(body["prefix"]) != protocol.frozen_prefix_sha256 or body[
        "maximum_provider_requests"
    ] != request_ceiling(protocol):
        raise ValueError("experiment_prefix_or_allowance_changed")
    return body, protocol

"""Explicit transactional archive of a completed synthetic demo run."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from database.schema import execution_runs, runtime_events, task_specs
from domain.tasks import TaskSpec
from runtime.artifacts import ArtifactStore


@dataclass
class RunBundle:
    task: TaskSpec
    manifest: dict
    events: list[dict]


def load_bundle(run_dir: Path) -> RunBundle:
    """Validate local lineage and artifact hashes before opening a transaction."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != "demo-run/0.2" or manifest["data_kind"] != "synthetic":
        raise ValueError("only synthetic demo-run/0.2 archives are supported")
    if manifest["status"] not in {"succeeded", "failed", "denied"}:
        raise ValueError("archive requires a terminal run")
    UUID(manifest["run_id"])
    store = ArtifactStore(run_dir)
    for key in (
        "input_artifact",
        "output_artifact",
        "model_input_artifact",
        "model_output_artifact",
    ):
        if manifest.get(key):
            store.read_bytes(manifest[key]["sha256"])
    request = store.read_json(manifest["input_artifact"]["sha256"])
    task = TaskSpec.model_validate(request["task"])
    if any(
        manifest[key] != getattr(task, key)
        for key in ("task_id", "release_id", "grant_id", "revision_id")
    ):
        raise ValueError("task and run lineage differ")
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if (
        not events
        or events[0]["event_type"] != "run_started"
        or events[-1]["event_type"] != "run_finished"
    ):
        raise ValueError("archive requires a complete event stream")
    for sequence, event in enumerate(events, 1):
        UUID(event["event_id"])
        UUID(event["call_id"])
        if event["sequence"] != sequence or any(
            event[key] != manifest[key]
            for key in (
                "run_id",
                "task_id",
                "release_id",
                "grant_id",
                "revision_id",
                "execution_generation",
                "data_kind",
            )
        ):
            raise ValueError("event order or lineage differs from run")
    if events[-1]["status"] != manifest["status"]:
        raise ValueError("final event status differs from run")
    return RunBundle(task, manifest, events)


def archive_run(engine: sa.Engine, run_dir: Path) -> dict:
    bundle = load_bundle(run_dir)
    task, manifest, events = bundle.task, bundle.manifest, bundle.events
    spec = task.model_dump(mode="json")
    # Context manager commits all rows together, or rolls back on any exception.
    with engine.begin() as connection:
        existing = connection.scalar(
            sa.select(task_specs.c.spec).where(task_specs.c.task_id == task.task_id)
        )
        if existing is None:
            connection.execute(
                task_specs.insert().values(
                    task_id=task.task_id, schema_version=task.schema_version, spec=spec
                )
            )
        elif existing != spec:
            raise ValueError("task_id already belongs to a different immutable task snapshot")
        connection.execute(
            execution_runs.insert().values(
                run_id=manifest["run_id"],
                task_id=task.task_id,
                release_id=task.release_id,
                grant_id=task.grant_id,
                revision_id=task.revision_id,
                status=manifest["status"],
                manifest=manifest,
            )
        )
        connection.execute(
            runtime_events.insert(),
            [
                {
                    key: event[key]
                    for key in ("event_id", "run_id", "call_id", "execution_generation", "sequence")
                }
                | {"payload": event}
                for event in events
            ],
        )
    return {
        "status": "archived",
        "run_id": manifest["run_id"],
        "event_count": len(events),
        "data_kind": "synthetic",
    }

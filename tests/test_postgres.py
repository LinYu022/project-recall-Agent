"""Real PostgreSQL checks in disposable, uniquely named schemas only."""

import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from database.archive import archive_run, load_bundle
from database.schema import execution_runs, metadata, runtime_events, task_specs
from tests.conftest import migrate

pytestmark = pytest.mark.postgres


def test_upgrade_matches_schema_and_roundtrip_downgrade(database):
    with database.begin() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0009_procedural_memory"
        )
        migrate(connection)  # Applying head again must not recreate tables.
        context = MigrationContext.configure(connection, opts={"compare_server_default": True})
        assert compare_metadata(context, metadata) == []
        migrate(connection, "base")
        assert not sa.inspect(connection).has_table("task_specs")
        migrate(connection)
        assert sa.inspect(connection).has_table("runtime_events")


@pytest.mark.parametrize(
    "scenario,status,tool_name",
    [
        ("success", "succeeded", "fake_query"),
        ("tool-error", "failed", "fake_query"),
        ("success", "denied", "forbidden"),
    ],
)
def test_archive_roundtrips_lineage_and_preserves_first_run(
    database, demo_run, scenario, status, tool_name
):
    run_dir = demo_run(scenario=scenario, tool_name=tool_name, with_model=True)
    bundle = load_bundle(run_dir)
    assert archive_run(database, run_dir)["status"] == "archived"
    with database.connect() as connection:
        manifest = connection.scalar(sa.select(execution_runs.c.manifest))
        assert manifest == bundle.manifest and manifest["status"] == status
        assert connection.scalar(sa.select(task_specs.c.spec)) == bundle.task.model_dump(
            mode="json"
        )
        events = connection.scalars(
            sa.select(runtime_events.c.payload).order_by(runtime_events.c.sequence)
        ).all()
        assert events == bundle.events
    with pytest.raises(sa.exc.IntegrityError):
        archive_run(database, run_dir)  # No silent overwrite on duplicate run IDs.
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(execution_runs)) == 1


def test_late_insert_failure_rolls_back_task_run_and_events(database, demo_run):
    first = demo_run("DEMO-FIRST")
    archive_run(database, first)
    collision_id = load_bundle(first).events[0]["event_id"]
    second = demo_run("DEMO-SECOND")
    bundle = load_bundle(second)
    bundle.events[-1]["event_id"] = collision_id
    (second / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in bundle.events), encoding="utf-8"
    )
    with pytest.raises(sa.exc.IntegrityError):
        archive_run(database, second)
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(task_specs)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(execution_runs)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(runtime_events)) == 4


def test_same_task_id_cannot_overwrite_a_different_task(database, demo_run):
    first = demo_run()
    archive_run(database, first)
    second = demo_run(scenario="empty")
    # Same task may have multiple executions.
    archive_run(database, second)
    with database.begin() as connection:
        connection.execute(task_specs.update().values(spec={"changed": True}))
    with pytest.raises(ValueError, match="immutable"):
        archive_run(database, demo_run())
    with database.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(execution_runs)) == 2


@pytest.mark.parametrize(
    "violation",
    ["missing-run", "duplicate-sequence", "zero-sequence", "zero-generation", "non-object-payload"],
)
def test_database_rejects_invalid_event_rows(database, demo_run, violation):
    run_dir = demo_run()
    archive_run(database, run_dir)
    event = load_bundle(run_dir).events[0]
    row = {key: event[key] for key in ("run_id", "call_id", "execution_generation", "sequence")}
    row.update(event_id=uuid4().hex, sequence=99, payload={})
    if violation == "missing-run":
        row["run_id"] = uuid4().hex
    elif violation == "duplicate-sequence":
        row["sequence"] = 1
    elif violation == "zero-sequence":
        row["sequence"] = 0
    elif violation == "zero-generation":
        row["execution_generation"] = 0
    else:
        row["payload"] = []
    with pytest.raises(sa.exc.IntegrityError), database.begin() as connection:
        connection.execute(runtime_events.insert().values(**row))


def test_online_migration_and_archive_cli_do_not_expose_credentials(database, demo_run):
    with database.connect() as connection:
        schema = connection.scalar(sa.text("SELECT current_schema()"))
    url = database.url.update_query_dict({"options": f"-csearch_path={schema}"})
    environment = os.environ | {"RECALL_DATABASE_URL": url.render_as_string(hide_password=False)}
    commands = [
        (["alembic", "upgrade", "head"], 0),
        (["alembic", "current"], 0),
    ]
    run_dir = demo_run(with_model=True)
    commands += [(["database", str(run_dir)], 0), (["database", str(run_dir)], 2)]
    for arguments, code in commands:
        result = subprocess.run(
            [sys.executable, "-m", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == code
        if database.url.password:
            assert database.url.password not in result.stdout + result.stderr
        if arguments == ["alembic", "current"]:
            assert "0009_procedural_memory" in result.stdout
        if code == 2:
            assert json.loads(result.stdout) == {
                "status": "archive_failed",
                "error_type": "IntegrityError",
            }

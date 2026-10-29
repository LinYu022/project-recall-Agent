"""Checks that need neither a database server nor external network access."""

import io
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from database.archive import load_bundle
from database.config import database_url
from runtime.artifacts import ArtifactIntegrityError

ROOT = Path(__file__).resolve().parents[1]


def test_database_config_requires_explicit_postgres_driver(monkeypatch):
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="required"):
        database_url()
    monkeypatch.setenv("RECALL_DATABASE_URL", "sqlite://")
    with pytest.raises(ValueError, match="postgresql"):
        database_url()
    monkeypatch.setenv("RECALL_DATABASE_URL", "postgresql+psycopg://test:secret@127.0.0.1/recall")
    assert database_url().database == "recall"
    assert "secret" not in str(database_url())


def test_migration_generates_postgres_sql_without_credentials(monkeypatch):
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE task_specs" in sql
    assert "JSONB" in sql and "TIMESTAMP WITH TIME ZONE" in sql
    assert "fk_event_run" in sql and "uq_event_run_sequence" in sql
    assert "0001_control_tables" in sql


def test_archive_rejects_broken_lineage_before_database_write(demo_run):
    run_dir = demo_run()
    assert len(load_bundle(run_dir).events) == 4
    path = run_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[1]["task_id"] = "OTHER-TASK"
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    with pytest.raises(ValueError, match="lineage"):
        load_bundle(run_dir)


def test_archive_checks_source_artifact_hash(demo_run):
    run_dir = demo_run()
    bundle = load_bundle(run_dir)
    digest = bundle.manifest["input_artifact"]["sha256"]
    (run_dir / "artifacts" / f"{digest}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        load_bundle(run_dir)

"""Shared synthetic run fixture; never calls external data or models."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from domain.tasks import TaskSpec
from models.adapter import FakeModelAdapter
from runtime.config import load_settings
from runtime.demo import run_demo
from tools.fake_query import FakeQueryTool

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo_run(tmp_path, monkeypatch):
    def create(
        task_id="DEMO-ARCHIVE", scenario="success", tool_name="fake_query", with_model=False
    ):
        monkeypatch.setenv("RECALL_ARTIFACT_DIR", str(tmp_path / "runs"))
        task = TaskSpec.model_validate_json(
            (ROOT / "examples/demo-task.json").read_text(encoding="utf-8")
        )
        task = task.model_copy(update={"task_id": task_id})
        settings = load_settings(ROOT / "examples/dev-readonly.toml")
        result = run_demo(
            task,
            settings,
            {"fake_query": FakeQueryTool(scenario)},
            scenario=scenario,
            tool_name=tool_name,
            model=FakeModelAdapter() if with_model else None,
        )
        return Path(result["run_dir"])

    return create


def migrate(connection, target="head"):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    if target == "base":
        command.downgrade(config, target)
    else:
        command.upgrade(config, target)


@pytest.fixture
def database():
    url = os.environ.get("RECALL_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "postgres tests require RECALL_TEST_DATABASE_URL; use a disposable test database"
        )
    schema = "test_" + uuid4().hex
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": f"-csearch_path={schema}"},
        )
        with engine.begin() as connection:
            migrate(connection)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            # schema is generated above, never taken from a user's URL or input.
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()

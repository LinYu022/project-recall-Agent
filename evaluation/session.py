"""An evaluation owns one persistent schema and a locked output directory."""

from contextlib import contextmanager

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@contextmanager
def session(url, experiment_id, *, mode, cleanup=False):
    if len(experiment_id) != 64 or any(c not in "0123456789abcdef" for c in experiment_id):
        raise ValueError("invalid_experiment_id")
    if mode not in {"scripted", "live"}:
        raise ValueError("invalid_evaluation_mode")
    schema = "evaluation_" + experiment_id[:24] + "_" + mode
    admin = sa.create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
    engine = None
    try:
        with admin.begin() as connection:
            if not sa.inspect(connection).has_schema(schema):
                connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(
            url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-csearch_path=" + schema},
        )
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine, schema
    finally:
        if engine:
            engine.dispose()
        if cleanup:
            with admin.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()

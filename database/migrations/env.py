"""Offline SQL generation and online PostgreSQL migrations."""

import sqlalchemy as sa
from alembic import context

import control.queue_schema  # noqa: F401 - register dispatch metadata
import control.schema  # noqa: F401 - register lifecycle metadata
import memory.episode_schema  # noqa: F401 - register episodic memory metadata
import memory.procedure_schema  # noqa: F401 - register procedure memory metadata
import memory.schema  # noqa: F401 - register semantic memory metadata
from database.config import database_url
from database.schema import metadata


def migrate(connection):
    context.configure(connection=connection, target_metadata=metadata, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(dialect_name="postgresql", target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    migrate(context.config.attributes["connection"])
else:
    engine = sa.create_engine(
        database_url(),
        poolclass=sa.pool.NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()

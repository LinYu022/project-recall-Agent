"""Database credentials come from the environment, never source files."""

import os

from sqlalchemy.engine import URL, make_url


def database_url() -> URL:
    raw = os.environ.get("RECALL_DATABASE_URL")
    if not raw:
        raise ValueError("RECALL_DATABASE_URL is required for online database operations")
    url = make_url(raw)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("only postgresql+psycopg is supported")
    return url

"""Archive local demo output: python -m database RUN_DIRECTORY."""

import argparse
import json
from pathlib import Path

import sqlalchemy as sa

from database.archive import archive_run
from database.config import database_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    engine = None
    try:
        engine = sa.create_engine(
            database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
        )
        result = archive_run(engine, args.run_directory)
    except (ValueError, KeyError, TypeError, OSError, sa.exc.SQLAlchemyError) as error:
        # Database exceptions can carry parameters or credentials: do not print them.
        print(json.dumps({"status": "archive_failed", "error_type": type(error).__name__}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

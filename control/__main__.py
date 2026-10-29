"""Start the protected local workflow service; credentials stay in environment variables."""

import argparse
import json
import os
from pathlib import Path

import sqlalchemy as sa
import uvicorn

from app.control_api import create_control_app
from control.dispatch import Dispatcher
from control.service import TaskControl
from database.config import database_url
from investigation.contracts import FixedConfig
from models.chat_api import ChatJSONModel
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.service import PublishedService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--context-mode", choices=["full_history", "structured"], default="structured"
    )
    args = parser.parse_args()
    tokens = json.loads(os.environ["RECALL_CONTROL_TOKEN_OWNERS"])
    engine = sa.create_engine(
        database_url(), hide_parameters=True, connect_args={"connect_timeout": 5}
    )
    try:
        service = PublishedService(
            Catalog(
                engine, BundleStore(Path(os.environ.get("RECALL_RELEASE_ROOT", ".local/releases")))
            ),
            Path(".local/control-traces"),
        )
        control = TaskControl(
            service,
            ChatJSONModel(),
            Path(".local/control"),
            config=FixedConfig(max_model_calls=6, context_mode=args.context_mode),
        )
        Dispatcher(control)
        uvicorn.run(
            create_control_app(control, tokens), host="127.0.0.1", port=args.port, access_log=False
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Serve the product. Demo profiles cannot create external model requests."""

import argparse
import json
import os
import re
import secrets
import threading
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
import uvicorn
from alembic import command
from alembic.config import Config

from control.dispatch import Dispatcher
from control.service import TaskControl
from control.worker import Worker
from investigation.contracts import FixedConfig
from portal.api import create_product_app
from portal.demo import DemoModel, synthetic_bundle
from portal.identity import implementation
from portal.postgres import portable_postgres
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from releases.service import PublishedService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=["synthetic-demo", "official-demo", "live"], default="synthetic-demo"
    )
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument("--root", type=Path, default=Path(".local/P1-17/product"))
    parser.add_argument("--postgres-bin", type=Path)
    parser.add_argument("--ui-root", type=Path, default=Path("frontend/dist"))
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    schema = os.environ.get("RECALL_PRODUCT_SCHEMA", "recall_product")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", schema):
        raise ValueError("invalid_product_schema")
    with ExitStack() as stack:
        url = (
            stack.enter_context(
                portable_postgres(
                    args.postgres_bin.resolve(), args.root / ("db-" + uuid4().hex[:8])
                )
            )
            if args.postgres_bin
            else os.environ["RECALL_DATABASE_URL"]
        )
        admin = sa.create_engine(url, hide_parameters=True)
        stack.callback(admin.dispose)
        with admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema, if_not_exists=True))
        engine = sa.create_engine(
            url, hide_parameters=True, connect_args={"options": f"-csearch_path={schema}"}
        )
        stack.callback(engine.dispose)
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        if args.profile == "synthetic-demo":
            pointer = args.root / "synthetic-release.json"
            if pointer.exists():
                candidate = json.loads(pointer.read_bytes())
                store = BundleStore(args.root / "fixtures/releases", allow_synthetic=True)
            else:
                store, candidate = synthetic_bundle(args.root / "fixtures")
                pointer.write_text(json.dumps(candidate), encoding="utf-8")
        else:
            index = json.loads(Path("examples/release-candidates.json").read_bytes())
            candidate, store = index["candidates"]["expanded"], BundleStore(Path(index["root"]))
        catalog = Catalog(engine, store)
        from database.schema import release_defaults

        with engine.begin() as connection:
            current = connection.scalar(sa.select(release_defaults.c.release_id))
        catalog.publish(
            PublishRequest(
                release_id=candidate["release_id"],
                manifest_sha256=candidate["manifest_sha256"],
                expected_default=current,
                retain_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        model: Any
        if args.profile == "live":
            from models.chat_api import ChatJSONModel

            model = ChatJSONModel()
        else:
            model = DemoModel()
        control = TaskControl(
            PublishedService(catalog, args.root / "traces"),
            model,
            args.root / "runs",
            config=FixedConfig(retrieval_method="bm25", context_mode="structured"),
        )
        dispatcher = Dispatcher(control)
        owners = json.loads(os.environ.get("RECALL_CONTROL_TOKEN_OWNERS", "{}"))
        if not owners:
            owners = {secrets.token_urlsafe(32): "product-demo"}
        session = args.root / "session.json"
        build_identity = implementation()
        session.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{args.port}",
                    "token": next(iter(owners)),
                    "profile": args.profile,
                }
            ),
            encoding="utf-8",
        )
        session.chmod(0o600)
        stopped = threading.Event()
        errors = args.root / "worker-errors.jsonl"

        def work():
            worker = Worker(dispatcher, "product-local-worker")
            while not stopped.is_set():
                try:
                    dispatcher.recover_expired()
                    result = worker.run_once()
                except Exception as error:
                    # Keep error type, never credentials or a private traceback in the public UI.
                    with errors.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"error_type": type(error).__name__}) + "\n")
                    result = None
                if result is None:
                    stopped.wait(0.5)

        @asynccontextmanager
        async def lifespan(app):
            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            try:
                yield
            finally:
                stopped.set()
                thread.join(timeout=30)

        app = create_product_app(
            control,
            owners,
            profile=args.profile,
            ui_root=args.ui_root,
            build_identity=build_identity,
        )
        app.router.lifespan_context = lifespan
        print(f"Product: http://127.0.0.1:{args.port}; local session file: {session}", flush=True)
        print(f"Profile: {args.profile}; quality acceptance: not_established", flush=True)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

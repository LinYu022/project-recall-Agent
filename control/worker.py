"""A local worker process; heartbeats extend leases, never stop an upstream request."""

from threading import Event, Thread

import sqlalchemy as sa

from control.access import row
from control.journal import Journal
from control.queue_schema import jobs
from control.schema import tasks
from runtime.durable import CURRENT_JOURNAL


class InjectedCrash(BaseException):
    """Tests emulate process death by skipping all normal completion handling."""


class Worker:
    def __init__(self, dispatcher, worker_id, *, fault=None, heartbeat=True):
        self.dispatcher, self.worker_id = dispatcher, worker_id
        self.fault, self.heartbeat_enabled = fault, heartbeat

    def run_once(self, *, job_id=None):
        claim = self.dispatcher.claim(self.worker_id, job_id=job_id)
        if claim is None:
            return None
        return self.run_claim(claim)

    def run_claim(self, claim):
        stopped = Event()

        def heartbeat():
            while not stopped.wait(min(5, claim.lease_seconds / 3)):
                try:
                    self.dispatcher.heartbeat(claim)
                except Exception:
                    return

        thread = Thread(target=heartbeat, daemon=True) if self.heartbeat_enabled else None
        if thread:
            thread.start()
        journal = Journal(self.dispatcher, claim, self.dispatcher.control.model, fault=self.fault)
        token = CURRENT_JOURNAL.set(journal)
        try:
            result = self.dispatcher.control._run_execution(
                claim.task["owner_id"],
                claim.task,
                claim.revision,
                claim.plan,
                claim.access,
                model=journal,
                config=claim.config,
            )
            if self.fault:
                self.fault("after_publication", "workflow", -1)
            self._finish(claim)
            return {"job_id": claim.job_id, "task": result, "calls": journal.stats()}
        except Exception:
            self._finish(claim, failed=True)
            raise
        finally:
            CURRENT_JOURNAL.reset(token)
            stopped.set()
            if thread:
                thread.join(timeout=2)

    def _finish(self, claim, *, failed=False):
        from control.queue_schema import calls

        with self.dispatcher.catalog.transaction() as connection:
            job = row(connection, jobs, "job_id", claim.job_id)
            task = row(connection, tasks, "task_id", claim.task["task_id"])
            # Only finish this claim. A newer worker may already own this job.
            if job["execution_id"] != claim.access.execution_id:
                return
            uncertain = connection.scalar(
                sa.select(calls.c.call_id).where(
                    calls.c.job_id == claim.job_id,
                    calls.c.state.in_(["reserved", "unknown"]),
                    calls.c.kind == "model",
                )
            )
            if task["state"] == "COMPLETED":
                state = "completed"
            elif task["state"] == "CANCELLED":
                state = "cancelled"
            elif uncertain or task["state"] in {"PAUSED", "WAITING_INPUT", "WAITING_APPROVAL"}:
                state = "waiting_input"
            else:
                state = "failed"
            connection.execute(
                jobs.update()
                .where(jobs.c.job_id == claim.job_id)
                .values(
                    state=state,
                    last_error="unknown_model_output"
                    if uncertain
                    else "execution_failed"
                    if failed or state == "failed"
                    else None,
                )
            )


def main():
    import argparse
    import os
    import socket
    import time
    from pathlib import Path

    from control.dispatch import Dispatcher
    from control.service import TaskControl
    from database.config import database_url
    from investigation.contracts import FixedConfig
    from models.chat_api import ChatJSONModel
    from releases.bundle import BundleStore
    from releases.catalog import Catalog
    from releases.service import PublishedService

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--context-mode", choices=["structured", "full_history"], default="structured"
    )
    parser.add_argument("--worker-id", default=socket.gethostname() + "-" + str(os.getpid()))
    args = parser.parse_args()
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
        dispatch = Dispatcher(
            TaskControl(
                service,
                ChatJSONModel(),
                Path(".local/control"),
                config=FixedConfig(max_model_calls=6, context_mode=args.context_mode),
            )
        )
        worker = Worker(dispatch, args.worker_id)
        while True:
            result = worker.run_once()
            if args.once:
                break
            if result is None:
                time.sleep(1)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

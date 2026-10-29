"""Deterministic fault experiments, separate from natural-task model quality."""

from datetime import timedelta

from control.contracts import Submission
from control.dispatch import Dispatcher, db_now
from control.queue_contracts import Enqueue
from control.queue_schema import jobs
from control.schema import executions
from control.worker import InjectedCrash, Worker


def recovery_trial(control, owner, task_id, plan, mode):
    if mode not in {"normal", "saved_tool_recovery", "recovery_disabled", "unknown_model"}:
        raise ValueError("unknown_challenge")
    ready = control.submit(owner, Submission(task_id=task_id, plan=plan))
    dispatch = control.dispatcher or Dispatcher(control)
    job = dispatch.enqueue(
        owner, task_id, Enqueue(expected_version=ready["version"], idempotency_key=mode)
    )
    triggered = False

    def fault(stage, kind, ordinal):
        nonlocal triggered
        if (
            mode == "unknown_model" and stage == "after_response_before_save" and kind == "model"
        ) or (
            mode in {"saved_tool_recovery", "recovery_disabled"}
            and stage == "after_settle"
            and kind == "tool"
            and ordinal == 0
        ):
            triggered = True
            raise InjectedCrash()

    claim = dispatch.claim("challenge-old", job_id=job["job_id"])
    try:
        result = Worker(dispatch, "challenge-old", fault=fault, heartbeat=False).run_claim(claim)
    except InjectedCrash:
        result = None
        with control.catalog.engine.begin() as connection:
            connection.execute(
                executions.update()
                .where(executions.c.execution_id == claim.access.execution_id)
                .values(lease_until=db_now(connection) - timedelta(seconds=1))
            )
        if mode == "recovery_disabled":
            current = control.view(owner, task_id)
            control.change_state(owner, task_id, current["version"], "cancel")
        else:
            dispatch.recover_expired()
            with control.catalog.engine.begin() as connection:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.job_id == job["job_id"])
                    .values(available_at=db_now(connection) - timedelta(seconds=1))
                )
            if mode == "saved_tool_recovery":
                result = Worker(dispatch, "challenge-new", heartbeat=False).run_once(
                    job_id=job["job_id"]
                )
    task = control.view(owner, task_id)
    state = dispatch.view(owner, task_id)
    return {
        "mode": mode,
        "classification": "real_worker_SQL_and_artifacts_same_process_fault_injection",
        "semantic_trigger": "first_completed_lookup"
        if mode != "unknown_model"
        else "model_response_received_before_durable_save",
        "fault_triggered": triggered,
        "task_state": task["state"],
        "safely_stopped": task["state"] in {"CANCELLED", "PAUSED"},
        "formal_answer_correct": None,
        "published": task["published_report"] is not None,
        "valid_execution_terminal": task["state"] == "COMPLETED",
        "calls": result["calls"] if result else None,
        "budget": state["resources"],
        "jobs": state["jobs"],
        "new_external_requests": 0,
    }

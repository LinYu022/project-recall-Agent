"""One synchronous synthetic call with development guards and audit events."""

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Mapping
from uuid import uuid4

from pydantic import ValidationError

from domain.tasks import TaskSpec
from models.adapter import ModelAdapter
from runtime.artifacts import ArtifactStore
from runtime.config import DevSettings
from runtime.model_call import call_model, model_denial
from tools.fake_query import FIXTURE_SCOPE, QueryResult, QueryTool


def _denial(
    task: TaskSpec, settings: DevSettings, tool_name: str, registry: Mapping[str, QueryTool]
) -> str | None:
    if tool_name not in settings.allowed_tools or tool_name not in registry:
        return "tool_not_allowed"
    if task.status != "ready" or task.split != "exploration":
        return "demo_task_not_ready"
    if (task.release_id, task.grant_id, task.revision_id) != (
        settings.release_id,
        settings.grant_id,
        settings.revision_id,
    ):
        return "context_not_granted"
    if (
        task.time.mode != "snapshot_analysis"
        or task.task_kind != "record_lookup"
        or task.scope != FIXTURE_SCOPE
    ):
        return "scope_not_granted"
    if not set(task.allowed_source_ids) <= set(settings.allowed_source_ids):
        return "source_not_granted"
    if settings.max_tool_calls < 1:
        return "tool_call_limit_reached"
    return None


def run_demo(
    task: TaskSpec,
    settings: DevSettings,
    registry: Mapping[str, QueryTool],
    *,
    tool_name: str = "fake_query",
    scenario: str = "success",
    model: ModelAdapter | None = None,
) -> dict:
    started = perf_counter()
    run_id, call_id = uuid4().hex, uuid4().hex
    run_dir = settings.artifact_dir / run_id
    store = ArtifactStore(run_dir)
    input_ref = store.put_json(
        {
            "schema_version": "demo-request/0.2",
            "task": task.model_dump(mode="json"),
            "tool": tool_name,
            "scenario": scenario,
            "requested_model": model.identity.model_dump(mode="json") if model else None,
            "configuration": settings.model_dump(mode="json", exclude={"artifact_dir"}),
        }
    )
    context = {
        "run_id": run_id,
        "task_id": task.task_id,
        "call_id": call_id,
        "execution_generation": 1,
        "release_id": task.release_id,
        "grant_id": task.grant_id,
        "revision_id": task.revision_id,
        "data_kind": "synthetic",
        "tool": tool_name,
    }
    sequence = 0

    def emit(event_type: str, **details) -> None:
        nonlocal sequence
        sequence += 1
        event = {
            "schema_version": "demo-event/0.2",
            **context,
            "event_id": uuid4().hex,
            "sequence": sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            **details,
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    emit(
        "run_started",
        input_artifact=input_ref,
        input_summary={"task_kind": task.task_kind, "source_count": len(task.allowed_source_ids)},
    )
    error_code = _denial(task, settings, tool_name, registry)
    if error_code is None and model is not None:
        error_code = model_denial(model, settings)
    model_result = {
        "model_call_id": None,
        "model_calls_used": 0,
        "model": model.identity.model_dump(mode="json") if model else None,
        "model_input_artifact": None,
        "model_output_artifact": None,
        "summary": None,
        "usage": None,
    }
    output_ref, record_count, calls_used = None, None, 0
    if error_code:
        status = "denied"
        emit("call_denied", error_code=error_code)
    else:
        calls_used = 1
        emit("call_started", input_artifact=input_ref)
        call_started_at = perf_counter()
        try:
            raw = registry[tool_name].query(task)
        except Exception:
            error_code = "tool_error"
        else:
            output_ref = store.put_json(raw)
            try:
                output = QueryResult.model_validate(raw)
                if any(
                    record.source_id not in task.allowed_source_ids for record in output.records
                ):
                    error_code = "tool_output_out_of_scope"
                else:
                    record_count = len(output.records)
            except ValidationError:
                error_code = "tool_output_invalid"
        status = "failed" if error_code else "succeeded"
        emit(
            "call_failed" if error_code else "call_succeeded",
            error_code=error_code,
            output_artifact=output_ref,
            record_count=record_count,
            elapsed_ms=round((perf_counter() - call_started_at) * 1000, 3),
        )
        if error_code is None and model is not None:
            model_result = call_model(task, output, model, store, emit)
            error_code = model_result.pop("error_code")
            if error_code:
                status = "failed"
    elapsed_ms = round((perf_counter() - started) * 1000, 3)
    emit(
        "run_finished",
        status=status,
        calls_used=calls_used,
        model_calls_used=model_result["model_calls_used"],
        error_code=error_code,
        elapsed_ms=elapsed_ms,
    )
    result = {
        "schema_version": "demo-run/0.2",
        **context,
        **model_result,
        "status": status,
        "calls_used": calls_used,
        "error_code": error_code,
        "record_count": record_count,
        "input_artifact": input_ref,
        "output_artifact": output_ref,
        "elapsed_ms": elapsed_ms,
        "run_dir": run_dir.as_posix(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result

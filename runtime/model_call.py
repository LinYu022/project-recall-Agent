"""Audit one model call, preserving failed raw outputs for inspection."""

from time import perf_counter
from typing import Callable
from uuid import uuid4

from pydantic import ValidationError

from domain.tasks import TaskSpec
from models.adapter import ModelAdapter, ModelInput, ModelOutput, SourceSnippet
from runtime.artifacts import ArtifactStore
from runtime.config import DevSettings
from tools.fake_query import QueryResult


def model_denial(model: ModelAdapter, settings: DevSettings) -> str | None:
    identity = model.identity
    if (
        identity.provider != "fake"
        or not identity.is_simulated
        or identity.model_id not in settings.allowed_models
    ):
        return "model_not_allowed"
    if settings.max_model_calls < 1:
        return "model_call_limit_reached"
    return None


def call_model(
    task: TaskSpec, output: QueryResult, model: ModelAdapter, store: ArtifactStore, emit: Callable
) -> dict:
    call_id = uuid4().hex
    identity = model.identity.model_dump(mode="json")
    request = ModelInput(
        task_id=task.task_id,
        release_id=output.release_id,
        question=task.question,
        records=[
            SourceSnippet(source_id=r.source_id, record_id=r.record_id, text=r.summary)
            for r in output.records
        ],
    )
    input_ref = store.put_json(
        {
            "schema_version": "model-request/0.1",
            "model": identity,
            "request": request.model_dump(mode="json"),
        }
    )
    emit("model_call_started", call_id=call_id, tool=None, model=identity, input_artifact=input_ref)
    started = perf_counter()
    output_ref, summary, error_code = None, None, None
    try:
        raw = model.generate(request)
    except Exception:
        error_code = "model_error"
    else:
        output_ref = store.put_json(raw)
        try:
            parsed = ModelOutput.model_validate(raw)
            seen_sources = {r.source_id for r in output.records}
            if not set(parsed.cited_source_ids) <= seen_sources or (
                seen_sources and not parsed.cited_source_ids
            ):
                error_code = "model_citation_invalid"
            else:
                summary = parsed.model_dump(mode="json")
        except ValidationError:
            error_code = "model_output_invalid"
    emit(
        "model_call_failed" if error_code else "model_call_succeeded",
        call_id=call_id,
        tool=None,
        model=identity,
        error_code=error_code,
        output_artifact=output_ref,
        elapsed_ms=round((perf_counter() - started) * 1000, 3),
    )
    return {
        "model_call_id": call_id,
        "model_calls_used": 1,
        "model": identity,
        "model_input_artifact": input_ref,
        "model_output_artifact": output_ref,
        "summary": summary,
        "usage": None,
        "error_code": error_code,
    }

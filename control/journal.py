"""Deterministic replay of completed steps; unknown model requests are never resubmitted."""

import sqlalchemy as sa

from control.access import AccessDenied, validate
from control.dispatch import db_now
from control.queue_contracts import vector
from control.queue_schema import calls, jobs
from ingestion.records import content_hash
from models.chat_api import ModelCallFailed
from runtime.artifacts import ArtifactIntegrityError


def canonical_input(kind, value):
    import copy

    value = copy.deepcopy(value)
    if kind == "model":
        # Only the execution generation changes on recovery. Plan/grant/release remain pinned.
        binding = value.get("payload", {}).get("authorization_limits", {}).get("binding")
        if binding and "epoch" in binding:
            binding["epoch"] = "CURRENT-VALIDATED-EPOCH"
        delegation = value.get("payload", {}).get("delegation")
        if delegation and "epoch" in delegation:
            delegation["epoch"] = "CURRENT-VALIDATED-EPOCH"
    return value


class Journal:
    def __init__(
        self,
        dispatcher,
        claim,
        delegate,
        *,
        fault=None,
        lane=0,
        allocation=None,
        gate_callback=None,
    ):
        self.dispatcher, self.claim, self.delegate = dispatcher, claim, delegate
        self.identity = delegate.identity
        self.store = dispatcher.store(claim.job_id)
        self.lane, self.allocation, self.gate_callback = lane, allocation, gate_callback
        self.ordinal = lane * 100
        self.children = []
        self.new_model_calls = self.replayed_model_calls = self.new_tool_calls = (
            self.replayed_tool_calls
        ) = 0
        self.fault = fault or (lambda stage, kind, ordinal: None)

    def _gate(self):
        if self.gate_callback:
            self.gate_callback()
        with self.dispatcher.catalog.engine.begin() as connection:
            validate(connection, self.claim.access, "research", db_now(connection))
        from memory.runtime import CURRENT_MEMORY

        memory = CURRENT_MEMORY.get()
        if memory:
            # Live primary status at every step. Full domain-source replay is
            # separately required before each generation and atomic publication.
            memory.validate(verify_domain=False)
        from memory.episode_runtime import CURRENT_HISTORY

        history = CURRENT_HISTORY.get()
        if history:
            history.validate()
        from memory.procedure_runtime import CURRENT_SKILL

        skill = CURRENT_SKILL.get()
        if skill:
            skill.validate()

    def _call(self, kind, value, requested, operation):
        self._gate()
        ordinal = self.ordinal
        self.ordinal += 1
        fingerprint = content_hash([kind, canonical_input(kind, value)])
        with self.dispatcher.catalog.engine.begin() as connection:
            previous = (
                connection.execute(
                    sa.select(calls)
                    .where(calls.c.job_id == self.claim.job_id, calls.c.ordinal == ordinal)
                    .order_by(calls.c.attempt.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if previous:
            if previous["kind"] != kind or previous["fingerprint"] != fingerprint:
                raise ArtifactIntegrityError("recovery_step_input_changed")
            if previous["state"] in {"completed", "error"}:
                saved = self.store.read_json(previous["output_artifact"]["sha256"])
                if kind == "model":
                    self.replayed_model_calls += 1
                else:
                    self.replayed_tool_calls += 1
                self._gate()
                if previous["state"] == "error":
                    if saved["error_type"] == "TimeoutError":
                        raise TimeoutError("recorded_readonly_timeout")
                    raise ModelCallFailed(saved["error_code"], saved.get("diagnostics"))
                return saved["result"] | {"replayed": True} if kind == "model" else saved["result"]
            if kind == "model":
                raise AccessDenied("unknown_model_output_cannot_be_retried")
        input_ref = self.store.put_json({"kind": kind, "input": value})
        call_id = self.dispatcher.reserve(
            self.claim,
            ordinal,
            kind,
            fingerprint,
            requested,
            input_ref,
            lane=self.lane,
            allocation=self.allocation,
        )
        self.fault("after_reserve", kind, ordinal)
        if kind == "model":
            self.new_model_calls += 1
        else:
            self.new_tool_calls += 1
        try:
            result = operation()
        except Exception as error:
            diagnostics = error.diagnostics if isinstance(error, ModelCallFailed) else None
            usage = (diagnostics or {}).get("usage")
            code = error.code if isinstance(error, ModelCallFailed) else type(error).__name__
            ref = self.store.put_json(
                {"error_type": type(error).__name__, "error_code": code, "diagnostics": diagnostics}
            )
            uncertain = kind == "model" and not diagnostics
            self.dispatcher.settle(
                self.claim, call_id, output_ref=ref, usage=usage, error_code=code, unknown=uncertain
            )
            if uncertain:
                if self.lane:
                    # An isolated branch stops; the coordinator may still deliver a partial
                    # result using the other branch while this reservation remains charged.
                    raise ModelCallFailed("delegated_model_response_unknown") from None
                with self.dispatcher.catalog.transaction() as connection:
                    connection.execute(
                        jobs.update()
                        .where(
                            jobs.c.job_id == self.claim.job_id,
                            jobs.c.execution_id == self.claim.access.execution_id,
                        )
                        .values(state="waiting_input", last_error="model_response_unknown")
                    )
                raise AccessDenied("model_response_unknown_requires_review") from None
            raise
        self.fault("after_response_before_save", kind, ordinal)
        ref = self.store.put_json({"result": result})
        self.dispatcher.settle(
            self.claim,
            call_id,
            output_ref=ref,
            usage=result.get("usage") if kind == "model" else None,
        )
        self.fault("after_settle", kind, ordinal)
        self._gate()
        return result

    def generate(self, system, payload, *, max_tokens):
        import json

        value = {
            "system": system,
            "payload": payload,
            "max_tokens": max_tokens,
            "model": self.identity,
        }
        requested = vector(
            model_calls=1,
            prompt_characters=len(system + json.dumps(payload, ensure_ascii=False)),
            completion_tokens=max_tokens,
        )
        return self._call(
            "model",
            value,
            requested,
            lambda: self.delegate.generate(system, payload, max_tokens=max_tokens),
        )

    def tool(self, name, arguments, operation):
        return self._call(
            "tool", {"tool": name, "arguments": arguments}, vector(tool_calls=1), operation
        )

    def stats(self):
        return {
            k: getattr(self, k) + sum(c.stats()[k] for c in self.children)
            for k in (
                "new_model_calls",
                "replayed_model_calls",
                "new_tool_calls",
                "replayed_tool_calls",
            )
        }

    def fork(self, lane, *, allocation=None, gate_callback=None):
        if (
            type(lane) is not int
            or not 1 <= lane <= 99
            or any(c.lane == lane for c in self.children)
        ):
            raise ValueError("invalid_or_reused_journal_lane")
        child = Journal(
            self.dispatcher,
            self.claim,
            self.delegate,
            fault=self.fault,
            lane=lane,
            allocation=allocation,
            gate_callback=gate_callback,
        )
        self.children.append(child)
        return child

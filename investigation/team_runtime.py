"""Per-run aggregate accounting and scoped branches on the parent's durable call ledger."""

from threading import Event, RLock
from time import perf_counter

from control.queue_contracts import add, fits, vector
from models.chat_api import ModelCallFailed


class TeamBudget:
    def __init__(self, config, simulated):
        self.limits = vector(
            model_calls=config.max_model_calls,
            tool_calls=config.max_tool_calls,
            prompt_characters=config.total_prompt_characters,
            completion_tokens=config.total_generation_tokens,
        )
        self.used = vector()
        self.lock = RLock()
        self.started = perf_counter()
        self.wall_seconds = config.wall_seconds
        self.simulated = simulated
        self.tools = []
        self.models = []

    def reserve(self, request):
        with self.lock:
            if perf_counter() - self.started >= self.wall_seconds:
                raise ModelCallFailed("wall_budget_exhausted")
            if not fits(self.limits, self.used, vector(), request):
                raise ModelCallFailed("shared_resource_budget_exhausted")
            self.used = add(self.used, request)

    def release_completion(self, reserved, response):
        usage = response.get("usage")
        count = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if type(count) is int and 0 <= count <= reserved:
            with self.lock:
                self.used["completion_tokens"] -= reserved - count

    def snapshot(self):
        with self.lock:
            return {
                "model_calls": self.used["model_calls"],
                "tool_calls": self.used["tool_calls"],
                "prompt_characters": self.used["prompt_characters"],
                "completion_tokens_reserved_or_used": self.used["completion_tokens"],
                "accounting": "logical_run_including_recorded_replay_real_new_requests_live_in_dispatch_ledger",
                "actual_provider_usage": None
                if self.simulated
                else [m.get("usage") for m in self.models if not m.get("replayed")],
                "estimated_cost": None,
                "elapsed_ms": (perf_counter() - self.started) * 1000,
            }


class StopToken:
    def __init__(self, seconds):
        self.cancelled = Event()
        self.deadline = perf_counter() + seconds

    def check(self):
        if self.cancelled.is_set() or perf_counter() >= self.deadline:
            raise ModelCallFailed("delegated_branch_timeout")


class TeamCalls:
    def __init__(self, budget, delegate, role, token=None):
        self.budget, self.delegate, self.role, self.token = budget, delegate, role, token
        self.identity = delegate.identity

    def check(self):
        if self.token:
            self.token.check()

    def generate(self, system, payload, *, max_tokens):
        import json

        self.check()
        requested = vector(
            model_calls=1,
            prompt_characters=len(system + json.dumps(payload, ensure_ascii=False)),
            completion_tokens=max_tokens,
        )
        self.budget.reserve(requested)
        entry = {"role": self.role, "reserved": max_tokens, "status": "started"}
        with self.budget.lock:
            self.budget.models.append(entry)
        try:
            result = self.delegate.generate(system, payload, max_tokens=max_tokens)
        except Exception:
            with self.budget.lock:
                entry["status"] = "failed"
            raise
        self.budget.release_completion(max_tokens, result)
        with self.budget.lock:
            entry.update(
                status="completed", usage=result.get("usage"), replayed=bool(result.get("replayed"))
            )
        self.check()
        return result

    def tool(self, name, args, operation):
        self.check()
        self.budget.reserve(vector(tool_calls=1))
        entry = {"tool": name, "role": self.role, "status": "started"}
        with self.budget.lock:
            self.budget.tools.append(entry)
        tick = perf_counter()
        try:
            result = (
                self.delegate.tool(name, args, operation)
                if hasattr(self.delegate, "tool")
                else operation()
            )
            with self.budget.lock:
                entry["status"] = "completed"
            self.check()
            return result
        except Exception as error:
            with self.budget.lock:
                entry.update(
                    status="failed", error_code=getattr(error, "code", type(error).__name__)
                )
            raise
        finally:
            with self.budget.lock:
                entry["elapsed_ms"] = (perf_counter() - tick) * 1000

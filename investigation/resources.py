"""Shared per-run limits; no provider retries or claims of interrupting in-flight work."""

import json
from time import perf_counter

from models.chat_api import ModelCallFailed


class RunBudget:
    def __init__(self, config, *, started=None, clock=perf_counter):
        self.config, self.clock = config, clock
        self.started = clock() if started is None else started
        self.model_calls = 0
        self.prompt_characters = 0
        self.completion_reserved = 0
        self.completion_reported = 0
        self.usage_complete = True
        self.usages = []

    def check_time(self):
        if self.clock() - self.started >= self.config.wall_seconds:
            raise ModelCallFailed("wall_budget_exhausted")

    def reserve_model(self, system, payload, requested):
        self.check_time()
        if self.model_calls >= self.config.max_model_calls:
            raise ModelCallFailed("model_call_budget_exhausted")
        size = len(system + json.dumps(payload, ensure_ascii=False))
        if (
            size > self.config.prompt_characters
            or self.prompt_characters + size > self.config.total_prompt_characters
        ):
            raise ModelCallFailed("prompt_budget_exhausted")
        remaining = self.config.total_generation_tokens - self.completion_reserved
        limit = min(requested, remaining)
        if limit < 128:
            raise ModelCallFailed("completion_budget_exhausted")
        self.model_calls += 1
        self.prompt_characters += size
        self.completion_reserved += limit
        return limit

    def record_model(self, response, reserved):
        usage = response.get("usage")
        value = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if type(value) is int and 0 <= value <= reserved:
            self.completion_reserved -= reserved - value
            self.completion_reported += value
        else:
            self.usage_complete = (
                False  # Keep the full reservation; never invent token consumption.
            )
        self.usages.append(usage)

    def snapshot(self):
        keys = ("prompt_tokens", "completion_tokens", "total_tokens")
        totals = {
            k: sum(u[k] for u in self.usages)
            if self.usages
            and all(isinstance(u, dict) and type(u.get(k)) is int for u in self.usages)
            else None
            for k in keys
        }
        return {
            "model_calls": self.model_calls,
            "prompt_characters": self.prompt_characters,
            "completion_tokens_reserved_or_used": self.completion_reserved,
            "provider_usage_totals": totals,
            "usage_complete": self.usage_complete and len(self.usages) == self.model_calls,
            "elapsed_ms": (self.clock() - self.started) * 1000,
            "wall_limit_is_checked_between_operations": True,
        }

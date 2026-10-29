"""Optional trusted journal hook; never supplied by model output or HTTP request fields."""

from contextvars import ContextVar
from typing import Any

CURRENT_JOURNAL: ContextVar[Any] = ContextVar("durable_journal", default=None)


def tool_call(name, arguments, operation):
    journal = CURRENT_JOURNAL.get()
    return journal.tool(name, arguments, operation) if journal is not None else operation()

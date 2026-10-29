from typing import Annotated

from pydantic import AwareDatetime, Field

from control.contracts import Versioned
from domain.tasks import Contract

RESOURCES = ("model_calls", "tool_calls", "prompt_characters", "completion_tokens")


class Enqueue(Versioned):
    idempotency_key: Annotated[
        str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    deadline: AwareDatetime | None = None
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=5)] = 3
    lease_seconds: Annotated[int, Field(strict=True, ge=2, le=300)] = 60


class Reconcile(Contract):
    call_id: str
    completion_tokens: Annotated[int, Field(strict=True, ge=0)]
    note: Annotated[str, Field(min_length=5, max_length=1000)]


def vector(**values):
    if any(k not in RESOURCES or type(v) is not int or v < 0 for k, v in values.items()):
        raise ValueError("invalid_resource_vector")
    return {k: values.get(k, 0) for k in RESOURCES}


def add(left, right, sign=1):
    return vector(**{k: left[k] + sign * right[k] for k in RESOURCES})


def fits(limits, used, held, requested):
    return all(used[k] + held[k] + requested[k] <= limits[k] for k in RESOURCES)

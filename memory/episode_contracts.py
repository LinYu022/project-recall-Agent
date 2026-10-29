"""History is navigation and failed-hypothesis context, never current business evidence."""

from typing import Annotated

from pydantic import AwareDatetime, Field

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from memory.contracts import ProjectID
from retrieval.contracts import SearchScope

TaskID = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]{0,79}$")]


class HistorySelection(Contract):
    project_id: ProjectID
    enabled: Annotated[bool, Field(strict=True)] = True
    allow_cross_release: Annotated[bool, Field(strict=True)] = False
    limit: Annotated[int, Field(strict=True, ge=1, le=3)] = 3


class Capture(Contract):
    task_id: TaskID
    project_id: ProjectID


class HistoryQuery(Contract):
    project_id: ProjectID
    release_id: Digest
    allowed_source_ids: Annotated[list[Text], Field(max_length=50)]
    scope: SearchScope
    query: Annotated[str, Field(max_length=1000)] = ""
    allow_cross_release: Annotated[bool, Field(strict=True)] = False
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 5
    before_episode_id: Text | None = None
    occurred_before: AwareDatetime | None = None
    time_scope: dict | None = None


class EventQuery(HistoryQuery):
    task_id: TaskID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 10


class Window(HistoryQuery):
    episode_id: Text
    event_id: Text
    radius: Annotated[int, Field(strict=True, ge=0, le=5)] = 2


class SourcePage(HistoryQuery):
    episode_id: Text
    evidence_id: Digest
    offset: Annotated[int, Field(strict=True, ge=0)] = 0
    page_size: Annotated[int, Field(strict=True, ge=1, le=4000)] = 2000


class HistoryChange(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    enabled: Annotated[bool, Field(strict=True)]


def project_for(plan):
    values = {
        v
        for v in (
            plan.project_id,
            plan.memory.project_id if plan.memory else None,
            plan.history.project_id if plan.history else None,
            plan.skill.project_id if plan.skill else None,
        )
        if v is not None
    }
    if len(values) > 1:
        raise ValueError("plan_memory_and_history_project_must_match")
    return next(iter(values), None)


def compatible_scope(current, historical):
    from control.contracts import scope_within

    for field in ("make", "model", "model_year"):
        left, right = getattr(current, field), getattr(historical, field)
        if isinstance(left, str):
            left = left.strip().upper()
        if isinstance(right, str):
            right = right.strip().upper()
        if left != right:
            return False
    return scope_within(current, historical)

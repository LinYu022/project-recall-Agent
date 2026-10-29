from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text, TimeScope
from ingestion.contracts import Digest
from investigation.contracts import InvestigationRequest
from memory.contracts import Selection
from memory.episode_contracts import HistorySelection, project_for
from memory.procedure_contracts import SkillSelection
from retrieval.contracts import SearchScope

State = Literal[
    "DRAFT",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "RUNNING",
    "PAUSED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
Operation = Literal["research", "generate", "publish"]
TaskID = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]{0,79}$")]


class Plan(Contract):
    release_id: Digest
    allowed_source_ids: Annotated[list[Text], Field(min_length=1, max_length=50)]
    scope: SearchScope
    request: InvestigationRequest
    hypotheses: Annotated[list[Text], Field(max_length=12)] = []
    steps: Annotated[list[Text], Field(max_length=12)] = []
    mode: Literal["PLAN", "EXECUTE"] = "PLAN"
    investigator: Literal["fixed", "single_agent", "parallel_tools", "multi_agent"] = "fixed"
    memory: Selection | None = None
    project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")] | None = None
    history: HistorySelection | None = None
    skill: SkillSelection | None = None

    @model_validator(mode="after")
    def one_project(self):
        project_for(self)
        return self


class Submission(Contract):
    task_id: TaskID
    plan: Plan
    parent_id: TaskID | None = None


class AuthorizationBoundary(Contract):
    release_id: Digest
    allowed_source_ids: Annotated[list[Text], Field(min_length=1)]
    scope: SearchScope
    operations: Annotated[list[Operation], Field(min_length=1)]
    time: TimeScope
    memory: Selection | None = None
    project_id: str | None = None
    history: HistorySelection | None = None
    skill: SkillSelection | None = None


class Versioned(Contract):
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class RevisionInput(Versioned):
    plan: Plan


class Approval(Versioned):
    question_id: Text
    revision_id: Text
    plan_sha256: Digest
    approve: Annotated[bool, Field(strict=True)]


class InputAnswer(Versioned):
    question_id: Text
    values: dict


class Research(Contract):
    operation: Literal["search", "count", "links"]
    query: Annotated[str, Field(min_length=1, max_length=1000)] = "scope research"
    components: list[Text] = []


def operations(mode):
    return ["research"] if mode == "PLAN" else ["research", "generate", "publish"]


def scope_within(candidate: SearchScope, boundary: SearchScope):
    """Exact constraints may be added; removing/changing an approved constraint widens scope."""
    for key in ("make", "model", "model_year", "campaign_number", "component", "production_date"):
        expected, actual = getattr(boundary, key), getattr(candidate, key)
        if isinstance(expected, str) and isinstance(actual, str):
            expected, actual = expected.strip().upper(), actual.strip().upper()
        if expected is not None and actual != expected:
            return False
    return all(
        candidate.configurations.get(k, "").upper() == v.upper()
        for k, v in boundary.configurations.items()
    )


def within(plan: Plan, grant: dict):
    return (
        plan.release_id == grant["release_id"]
        and set(plan.allowed_source_ids) <= set(grant["allowed_source_ids"])
        and scope_within(plan.scope, SearchScope.model_validate(grant["scope"]))
        and set(operations(plan.mode)) <= set(grant["operations"])
        and (
            plan.memory is None
            or not plan.memory.enabled
            or plan.memory.model_dump(mode="json") == grant.get("memory")
        )
        and (plan.project_id is None or plan.project_id == grant.get("project_id"))
        and (
            plan.history is None
            or not plan.history.enabled
            or plan.history.model_dump(mode="json") == grant.get("history")
        )
        and (
            plan.skill is None
            or not plan.skill.enabled
            or plan.skill.model_dump(mode="json") == grant.get("skill")
        )
    )


def missing_conditions(plan: Plan):
    required = set(plan.request.required_conditions)
    if (
        plan.request.intent in {"complaint_statistics", "applicability"}
        or not plan.scope.campaign_number
    ):
        required |= {"make", "model", "model_year"}
    if plan.request.intent == "applicability":
        required |= {"production_date", "configurations"}
    return sorted(k for k in required if not getattr(plan.scope, k))


TRANSITIONS = {
    "DRAFT": {"WAITING_INPUT", "WAITING_APPROVAL", "RUNNING", "PAUSED", "CANCELLED", "FAILED"},
    "WAITING_INPUT": {
        "DRAFT",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "PAUSED",
        "CANCELLED",
        "FAILED",
    },
    "WAITING_APPROVAL": {"DRAFT", "WAITING_INPUT", "PAUSED", "CANCELLED", "FAILED"},
    "RUNNING": {"PAUSED", "COMPLETED", "FAILED", "WAITING_INPUT", "CANCELLED"},
    "PAUSED": {"DRAFT", "WAITING_INPUT", "WAITING_APPROVAL", "RUNNING", "CANCELLED", "FAILED"},
    "FAILED": {"DRAFT", "WAITING_INPUT", "WAITING_APPROVAL", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}


def transition(current, target):
    if current != target and target not in TRANSITIONS[current]:
        raise ValueError("illegal_task_transition")
    return target


def active_grant(grant, now: datetime):
    return (
        grant is not None
        and grant["revoked_at"] is None
        and (grant["expires_at"] is None or grant["expires_at"] > now)
    )

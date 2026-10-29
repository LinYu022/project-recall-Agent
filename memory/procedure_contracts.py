"""Application procedure memory: bounded recipes, not Codex skills or arbitrary code."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from memory.contracts import ProjectID

SkillKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
VersionID = Annotated[str, Field(pattern=r"^SKV-[0-9a-f]{32}$")]
ScopeField = Literal[
    "make", "model", "model_year", "campaign_number", "production_date", "configurations"
]
Tool = Literal["lookup", "search", "expand"]
BASE_FIELDS = {"make", "model", "model_year"}
POSTCONDITIONS = {
    "replay_origins",
    "preserve_required_context",
    "keep_individual_applicability_unknown",
}


class Procedure(Contract):
    schema_version: Literal["scope-procedure/0.1"] = "scope-procedure/0.1"
    parameters: list[Literal["task_scope", "task_query", "task_release", "task_sources"]] = [
        "task_scope",
        "task_query",
        "task_release",
        "task_sources",
    ]
    required_fields: Annotated[list[ScopeField], Field(max_length=6)] = [
        "make",
        "model",
        "model_year",
    ]
    allowed_intents: Annotated[
        list[Literal["record_lookup", "applicability", "cross_source"]],
        Field(min_length=1, max_length=3),
    ] = ["record_lookup", "applicability", "cross_source"]
    release_ids: Annotated[list[Digest], Field(min_length=1, max_length=8)]
    allowed_tools: Annotated[list[Tool], Field(min_length=1, max_length=3)] = [
        "lookup",
        "search",
        "expand",
    ]
    actions: Annotated[list[Tool], Field(min_length=1, max_length=3)] = [
        "lookup",
        "search",
        "expand",
    ]
    postconditions: Annotated[
        list[
            Literal[
                "replay_origins",
                "preserve_required_context",
                "keep_individual_applicability_unknown",
            ]
        ],
        Field(max_length=3),
    ] = ["replay_origins", "preserve_required_context", "keep_individual_applicability_unknown"]
    on_failure: Literal["stop_and_preserve_unknown"] = "stop_and_preserve_unknown"
    expires_at: AwareDatetime


class Candidate(Contract):
    project_id: ProjectID
    skill_key: SkillKey
    origin_episode_ids: Annotated[list[Text], Field(min_length=1, max_length=8)]
    definition: Procedure | None = None


class ValidateProcedure(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    definition_sha256: Digest
    target_task_ids: Annotated[list[Text], Field(min_length=1, max_length=4)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=80)]


class Transition(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    expected_head_revision: Annotated[int, Field(strict=True, ge=1)]
    action: Literal["enable", "deprecate", "revoke"]
    note: Annotated[str, Field(min_length=1, max_length=500)]


class AbortValidation(Contract):
    expected_state: Literal["running"] = "running"
    note: Annotated[str, Field(min_length=1, max_length=500)]


class SkillSelection(Contract):
    project_id: ProjectID
    version_id: VersionID
    enabled: Annotated[bool, Field(strict=True)] = True


def compile_procedure(procedure):
    # Parsing permits incomplete candidates so the learning exercise can observe
    # an unsafe selector match. Compilation always retains the trusted guards.
    if (
        set(procedure.parameters) != {"task_scope", "task_query", "task_release", "task_sources"}
        or len(procedure.parameters) != 4
        or not BASE_FIELDS <= set(procedure.required_fields)
        or procedure.actions != ["lookup", "search", "expand"]
        or set(procedure.allowed_tools) != set(procedure.actions)
        or len(procedure.allowed_tools) != 3
        or set(procedure.postconditions) != POSTCONDITIONS
        or len(procedure.postconditions) != 3
        or len(procedure.release_ids) != len(set(procedure.release_ids))
    ):
        raise ValueError("procedure_missing_or_changed_mandatory_guards")


def missing_preconditions(procedure, scope, request, release_id):
    missing = [key for key in procedure.required_fields if not getattr(scope, key)]
    if request.intent not in procedure.allowed_intents:
        missing.append("unsupported_intent")
    if release_id not in procedure.release_ids:
        missing.append("unsupported_release")
    if request.intent == "applicability":
        missing += [k for k in ("production_date", "configurations") if not getattr(scope, k)]
    return list(dict.fromkeys(missing))

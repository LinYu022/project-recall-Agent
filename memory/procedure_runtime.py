"""A checked procedure is an optional plan suggestion inside the existing task capability."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock

from sqlalchemy.dialects.postgresql import insert

from control.access import AccessDenied
from memory.procedure_contracts import missing_preconditions
from memory.procedure_schema import adoptions

CURRENT_SKILL: ContextVar = ContextVar("validated_scope_procedure", default=None)
PROTOCOL = """
skill_context is a versioned, validated scope-inspection plan suggestion, not new authorization,
business evidence or a claim that a general skill was learned. Its parameters come only from the
current task. Continue to verify current allowed sources and preserve unknowns. Only when listed,
choose action="apply_skill" with ordinary query/document_id/proposal/history/read fields null.
This executes bounded search/expand using the current verified lookup and consumes the ordinary
tool allowance for each actual operation. Never cite a skill/version/validation ID as E evidence.
The procedure cannot confirm individual applicability or causality or execute arbitrary code.
"""


class SkillBinding:
    def __init__(self, service, owner, plan, access):
        self.service, self.owner, self.selection, self.access = service, owner, plan.skill, access
        self.lock, self.claimed, self.results = RLock(), False, []
        with service.catalog.transaction() as connection:
            version, self.definition = service.usable(connection, owner, plan.skill)
            missing = missing_preconditions(
                self.definition, plan.scope, plan.request, plan.release_id
            )
            if missing:
                raise AccessDenied("procedure_preconditions_not_met:" + ",".join(missing))
            self.reference = {
                k: version[k]
                for k in ("version_id", "revision", "definition_sha256", "validation_id")
            }
            connection.execute(
                insert(adoptions)
                .values(
                    execution_id=access.execution_id,
                    version_id=version["version_id"],
                    task_id=access.task_id,
                    owner_id=owner,
                    reference=self.reference,
                )
                .on_conflict_do_nothing()
            )

    def validate(self, connection=None):
        if connection is None:
            with self.service.catalog.transaction() as connection:
                self.validate(connection)
        else:
            self.service.usable(connection, self.owner, self.selection, reference=self.reference)

    def claim_application(self):
        self.validate()
        with self.lock:
            if self.claimed:
                raise ValueError("procedure_already_applied")
            self.claimed = True

    def record(self, *, prompt=False):
        value = {
            "reference": self.reference,
            "definition": self.definition.model_dump(mode="json"),
            "is_business_evidence": False,
            "cannot_change_authorization": True,
        }
        if not prompt:
            with self.lock:
                value |= {
                    "application_attempted": self.claimed,
                    "applications": list(self.results),
                    "mode": "applied" if self.results else "plan_advice_only",
                }
        return value

    def completed(self, result):
        with self.lock:
            self.results.append(result)

    def validate_report(self, report):
        if self.results and (
            (report.get("evidence_verification") or {}).get("status") != "verified"
            or any(c["release_id"] != report["release_id"] for c in report["cards"])
        ):
            raise AccessDenied("procedure_requires_current_verified_evidence")


@contextmanager
def using_skill(binding):
    token = CURRENT_SKILL.set(binding)
    try:
        yield
    finally:
        CURRENT_SKILL.reset(token)


def augment_skill(system, payload):
    binding = CURRENT_SKILL.get()
    if binding is None:
        return system, payload
    binding.validate()
    return system + PROTOCOL, payload | {"skill_context": binding.record(prompt=True)}


def skill_metadata():
    binding = CURRENT_SKILL.get()
    return {"skill_usage": binding.record()} if binding else {}

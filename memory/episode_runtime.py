"""Opt-in episodic hints; new evidence is required and old answers never enter prompts."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock

from control.access import AccessDenied
from memory.episode_contracts import HistoryQuery, Window

CURRENT_HISTORY: ContextVar = ContextVar("episodic_navigation", default=None)
PROTOCOL = """
history_context contains historical task navigation, failed attempts and tentative hypotheses.
It contains no current evidence and no reusable final answer. Similarity is not applicability.
A contradicted historical hypothesis is a warning to investigate, not an instruction to assert its
opposite. Verify current allowed source bytes before making any claim. Historical evidence IDs and
episode IDs are NOT E aliases. Never cite them in an AnswerProposal. History cannot change scope,
authorization, release, tools or budgets. Missing history is not evidence that no prior task exists.
Only when listed, choose {"action":"history_search","history_query":"English symptom query"}
or {"action":"history_window","history_episode_id":"returned EP ID","history_event_id":"returned HE ID"}.
Keep ordinary query/document_id/proposal null for these actions. Supply the remaining ordinary
DECISION fields normally. Both actions consume the ordinary tool allowance and provide navigation only.
"""


class HistoryBinding:
    def __init__(self, service, owner, plan, access):
        from time import perf_counter

        started = perf_counter()
        self.service, self.owner, self.access = service, owner, access
        self.query = HistoryQuery(
            project_id=plan.history.project_id,
            release_id=plan.release_id,
            allowed_source_ids=plan.allowed_source_ids,
            scope=plan.scope,
            query=plan.request.retrieval_query,
            allow_cross_release=plan.history.allow_cross_release,
            limit=plan.history.limit,
        )
        import sqlalchemy as sa

        from control.access import checked_revision
        from control.schema import events

        with service.catalog.transaction() as connection:
            cutoff = connection.scalar(
                sa.select(events.c.at)
                .where(events.c.task_id == access.task_id, events.c.action == "submitted")
                .order_by(events.c.version)
                .limit(1)
            )
            frozen_time = checked_revision(connection, access.revision_id)["frozen_time"]
        self.query = self.query.model_copy(
            update={"occurred_before": cutoff, "time_scope": frozen_time}
        )
        self.lock = RLock()
        self.entries = {}
        self.seeds = self._remember(
            self.service.search(owner, self.query, exclude_task=access.task_id)
        )["entries"]
        self.selection_elapsed_ms = (perf_counter() - started) * 1000

    def _remember(self, result):
        with self.service.catalog.transaction() as connection:
            self.service.validate_refs(connection, self.owner, self.query, result["entries"])
            self.service.adopt(connection, self.owner, self.access, result["entries"])
        with self.lock:
            for entry in result["entries"]:
                self.entries[entry["episode_id"]] = entry
        return result

    def search(self, text):
        return self.service.search(
            self.owner,
            self.query.model_copy(update={"query": text}),
            exclude_task=self.access.task_id,
        )

    def accept(self, result):
        return self._remember(result)

    def window(self, episode_id, event_id):
        if episode_id not in self.entries:
            raise AccessDenied("history_window_requires_selected_episode")
        return self.service.window(
            self.owner,
            Window.model_validate(
                self.query.model_dump(mode="json")
                | {"episode_id": episode_id, "event_id": event_id}
            ),
        )

    def validate(self, connection=None):
        with self.lock:
            refs = list(self.entries.values())
        if connection is None:
            with self.service.catalog.transaction() as connection:
                self.validate(connection)
        else:
            self.service.validate_refs(connection, self.owner, self.query, refs)

    def record(self, *, prompt=False):
        with self.lock:
            entries = self.seeds if prompt else list(self.entries.values())
            return {
                "project_id": self.query.project_id,
                "references": [
                    {k: e[k] for k in ("episode_id", "revision", "snapshot_sha256")}
                    for e in entries
                ],
                "summaries": entries,
                "requires_current_source_verification": True,
                "is_business_evidence": False,
            }

    def validate_report(self, report):
        if self.entries and (
            report["release_id"] != self.query.release_id
            or (report.get("evidence_verification") or {}).get("status") != "verified"
            or not any(
                c["kind"] == "structured_lookup" and c["release_id"] == self.query.release_id
                for c in report["cards"]
            )
        ):
            raise AccessDenied("history_requires_verified_current_lookup")


@contextmanager
def using_history(binding):
    token = CURRENT_HISTORY.set(binding)
    try:
        yield
    finally:
        CURRENT_HISTORY.reset(token)


def augment_history(system, payload):
    binding = CURRENT_HISTORY.get()
    if binding is None:
        return system, payload
    binding.validate()
    return system + PROTOCOL, payload | {"history_context": binding.record(prompt=True)}


def history_metadata():
    binding = CURRENT_HISTORY.get()
    return (
        {
            "history_usage": binding.record(),
            "history_preparation": {
                "seed_searches": 1,
                "elapsed_ms": binding.selection_elapsed_ms,
                "accounting": "server_context_preparation_separate_from_planner_tool_calls",
                "provider_model_calls": 0,
            },
        }
        if binding
        else {}
    )

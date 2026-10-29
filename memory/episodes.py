"""Permission-filtered episodic navigation over immutable task/report snapshots."""

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from control.access import AccessDenied, ControlConflict, checked_grant, checked_revision
from control.contracts import Plan, active_grant, within
from control.schema import events as task_events
from control.schema import executions
from ingestion.records import content_hash
from investigation.context import FrozenContext
from investigation.contracts import EvidenceCard
from investigation.evidence import verify_cards
from investigation.flow import redact
from investigation.prompt_context import text_page
from memory import episode_schema as db
from memory.episode_contracts import compatible_scope, project_for
from memory.episode_projection import report_events, summary
from memory.service import now, tokens
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


class EpisodeService:
    def __init__(self, control):
        self.control, self.catalog, self.memory = control, control.catalog, control.memory

    def _project(self, connection, owner, project):
        p = self.memory._project(connection, owner, project)
        if not p["enabled"] or not self.memory._state(connection, owner)["enabled"]:
            raise AccessDenied("history_memory_disabled")

    def _source(self, connection, owner, body):
        task = self.control._task(connection, body["task_id"], owner)
        grant = checked_grant(connection, body["grant_id"])
        plan = Plan.model_validate(checked_revision(connection, body["revision_id"])["body"])
        if (
            task["revision_id"] != body["revision_id"]
            or task["grant_id"] != body["grant_id"]
            or not active_grant(grant, now(connection))
            or not within(plan, grant["body"])
        ):
            raise AccessDenied("historical_task_authorization_changed")
        self.control._parent(connection, task, plan, delivery=True)
        if (
            body["release_id"] != plan.release_id
            or body["scope"] != plan.scope.model_dump(mode="json")
            or body["allowed_source_ids"] != plan.allowed_source_ids
        ):
            raise ArtifactIntegrityError("episode_scope_differs_from_source_plan")
        for index, ref in enumerate(body.get("reports", [])):
            execution = (
                connection.execute(
                    sa.select(executions).where(
                        executions.c.execution_id == ref["execution_id"],
                        executions.c.task_id == task["task_id"],
                    )
                )
                .mappings()
                .one()
            )
            if execution["private_report"] != {
                k: ref[k] for k in ("artifact_root", "report_artifact")
            }:
                raise ArtifactIntegrityError("historical_report_reference_changed")
            report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                ref["report_artifact"]["sha256"]
            )
            expected = [{**e, "report_index": index} for e in report_events(report)]
            if [e for e in body["events"] if e.get("report_index") == index] != expected:
                raise ArtifactIntegrityError("historical_projection_differs_from_original_report")
        return task, plan

    def _body(self, row):
        body = row["body"]
        if content_hash(body) != row["body_sha256"]:
            raise ArtifactIntegrityError("episode_snapshot_changed")
        if row["episode_id"] != "EP-" + content_hash(
            [row["owner_id"], row["project_id"], row["task_id"], row["task_version"]]
        ):
            raise ArtifactIntegrityError("episode_identity_changed")
        return body

    def capture(self, owner, value):
        with self.catalog.transaction() as connection:
            return self._capture(connection, owner, value.task_id, value.project_id)

    def _capture(self, connection, owner, task_id, project_id):
        self._project(connection, owner, project_id)
        task = self.control._task(connection, task_id, owner)
        if task["state"] not in {"COMPLETED", "FAILED", "CANCELLED", "PAUSED"}:
            raise ValueError("history_capture_requires_finished_or_interrupted_task")
        revision = checked_revision(connection, task["revision_id"])
        plan = Plan.model_validate(revision["body"])
        if project_for(plan) not in {None, project_id}:
            raise AccessDenied("task_belongs_to_another_memory_project")
        episode_id = "EP-" + content_hash([owner, project_id, task_id, task["version"]])
        previous = (
            connection.execute(sa.select(db.episodes).where(db.episodes.c.episode_id == episode_id))
            .mappings()
            .first()
        )
        if previous:
            self._source(connection, owner, self._body(previous))
            return self._brief(previous)
        milestones = [
            dict(e)
            for e in connection.execute(
                sa.select(task_events)
                .where(task_events.c.task_id == task_id)
                .order_by(task_events.c.version)
            ).mappings()
            if e["payload"].get("revision_id") == task["revision_id"]
        ]
        body = {
            "schema_version": "investigation-episode/0.1",
            "task_id": task_id,
            "revision_id": task["revision_id"],
            "grant_id": task["grant_id"],
            "task_state": task["state"],
            "scope": plan.scope.model_dump(mode="json"),
            "release_id": plan.release_id,
            "time": revision["frozen_time"],
            "allowed_source_ids": plan.allowed_source_ids,
            "question": redact(plan.request.question),
            "query": redact(plan.request.retrieval_query),
            "events": [],
            "reports": [],
            "evidence_index": [],
            "occurred_at": max(e["at"] for e in milestones).isoformat(),
            "milestone_order": "control_version",
            "report_event_order": "recorded_section_array_order_not_wall_clock",
            "outcome_is_not_semantic_verdict": True,
        }
        self._source(connection, owner, body)
        body["events"] = [
            {
                "kind": "lifecycle",
                "version": e["version"],
                "action": e["action"],
                "state": e["payload"].get("to_state"),
                "error_code": e["payload"].get("error_type"),
                "at": e["at"].isoformat(),
            }
            for e in milestones
        ]
        for execution in connection.execute(
            sa.select(executions)
            .where(
                executions.c.task_id == task_id,
                executions.c.revision_id == task["revision_id"],
                executions.c.grant_id == task["grant_id"],
            )
            .order_by(executions.c.epoch, executions.c.execution_id)
        ).mappings():
            body["events"].append(
                {
                    "kind": "execution",
                    "execution_id": execution["execution_id"],
                    "state": execution["state"],
                    "epoch": execution["epoch"],
                    "report_available": bool(execution["private_report"]),
                }
            )
            if execution["private_report"]:
                ref = execution["private_report"]
                report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                    ref["report_artifact"]["sha256"]
                )
                if (
                    report["release_id"] != plan.release_id
                    or report["request"]["task_id"] != revision["binding_task_id"]
                ):
                    raise ArtifactIntegrityError("episode_report_differs_from_execution_binding")
                index = len(body["reports"])
                body["reports"].append({"execution_id": execution["execution_id"], **ref})
                body["evidence_index"].extend(
                    {k: c[k] for k in ("evidence_id", "kind", "source_ids")}
                    for c in report["cards"]
                )
                body["events"].extend({**e, "report_index": index} for e in report_events(report))
        row = {
            "episode_id": episode_id,
            "owner_id": owner,
            "project_id": project_id,
            "task_id": task_id,
            "task_version": task["version"],
            "body": body,
            "body_sha256": content_hash(body),
            "revision": 1,
            "enabled": True,
        }
        connection.execute(db.episodes.insert().values(**row))
        for ordinal, event in enumerate(body["events"]):
            connection.execute(
                db.events.insert().values(
                    event_id="HE-" + content_hash([episode_id, ordinal, event]),
                    episode_id=episode_id,
                    ordinal=ordinal,
                    body=event,
                    body_sha256=content_hash(event),
                )
            )
        self.memory._event(
            connection,
            owner,
            "episode_captured",
            {"episode_id": episode_id, "body_sha256": row["body_sha256"]},
        )
        return self._brief(row)

    def _brief(self, row):
        body = self._body(row)
        events = [
            {
                "event_id": "HE-" + content_hash([row["episode_id"], i, e]),
                "kind": e["kind"],
                "ordinal": i,
            }
            for i, e in enumerate(body["events"])
            if e["kind"] in {"hypothesis", "counterevidence", "outcome", "conflict"}
        ]
        return {
            "episode_id": row["episode_id"],
            "revision": row["revision"],
            "snapshot_sha256": row["body_sha256"],
            **summary(body),
            "anchors": events[-6:],
            "historical_source_handles": body["evidence_index"][:12],
        }

    def _eligible(self, connection, owner, row, query, *, exclude_task=None):
        if not row["enabled"] or row["task_id"] == exclude_task:
            return False
        body = self._body(row)
        if query.occurred_before:
            from datetime import datetime

            if datetime.fromisoformat(body["occurred_at"]) > query.occurred_before:
                return False
        if query.time_scope is not None and query.time_scope != body["time"]:
            return False
        if not compatible_scope(query.scope, self._scope(body)):
            return False
        if not set(body["allowed_source_ids"]) <= set(query.allowed_source_ids):
            return False
        if body["release_id"] != query.release_id and not query.allow_cross_release:
            return False
        try:
            self._source(connection, owner, body)
        except AccessDenied:
            return False
        return True

    @staticmethod
    def _scope(body):
        from retrieval.contracts import SearchScope

        return SearchScope.model_validate(body["scope"])

    def _rows(self, connection, owner, query, *, exclude_task=None):
        self._project(connection, owner, query.project_id)
        rows = connection.execute(
            sa.select(db.episodes)
            .where(db.episodes.c.owner_id == owner, db.episodes.c.project_id == query.project_id)
            .order_by(db.episodes.c.captured_at.desc(), db.episodes.c.episode_id)
        ).mappings()
        return [
            dict(row)
            for row in rows
            if self._eligible(connection, owner, row, query, exclude_task=exclude_task)
        ]

    def search(self, owner, query, *, recent=False, exclude_task=None):
        with self.catalog.transaction() as connection:
            rows = self._rows(connection, owner, query, exclude_task=exclude_task)
            if not recent:
                wanted = tokens(query.query)
                rows = [r for r in rows if not wanted or wanted & self._tokens(r)]
                rows.sort(key=lambda r: (-len(wanted & self._tokens(r)), r["episode_id"]))
            if query.before_episode_id:
                position = next(
                    (i for i, r in enumerate(rows) if r["episode_id"] == query.before_episode_id),
                    None,
                )
                if position is None:
                    raise AccessDenied("history_cursor_outside_selection")
                rows = rows[position + 1 :]
            chosen = rows[: query.limit]
            return {
                "entries": [self._brief(r) for r in chosen],
                "next_before_episode_id": chosen[-1]["episode_id"]
                if len(rows) > query.limit
                else None,
                "selection_before_ranking": True,
                "is_business_evidence": False,
            }

    def _tokens(self, row):
        value = summary(self._body(row))
        return tokens(
            " ".join(
                [
                    value["question"],
                    value["query"],
                    *value["failure_labels"],
                    *(h["statement"] for h in value["negative_hypotheses"]),
                ]
            )
        )

    def _event_rows(self, connection, row):
        expected = self._body(row)["events"]
        rows = list(
            connection.execute(
                sa.select(db.events)
                .where(db.events.c.episode_id == row["episode_id"])
                .order_by(db.events.c.ordinal)
            ).mappings()
        )
        if len(rows) != len(expected):
            raise ArtifactIntegrityError("history_event_index_incomplete")
        for ordinal, (event, body) in enumerate(zip(rows, expected, strict=True)):
            if (
                event["ordinal"] != ordinal
                or event["body"] != body
                or event["body_sha256"] != content_hash(body)
                or event["event_id"] != "HE-" + content_hash([row["episode_id"], ordinal, body])
            ):
                raise ArtifactIntegrityError("history_event_index_changed")
        return [
            {
                "event_id": e["event_id"],
                "episode_id": e["episode_id"],
                "ordinal": e["ordinal"],
                **e["body"],
            }
            for e in rows
        ]

    def events(self, owner, query):
        with self.catalog.transaction() as connection:
            rows = self._rows(connection, owner, query)
            result = []
            for row in rows:
                if query.task_id and row["task_id"] != query.task_id:
                    continue
                for event in self._event_rows(connection, row):
                    if not query.query or tokens(query.query) & tokens(str(event)):
                        result.append(event)
            return {
                "events": result[: query.limit],
                "has_more": len(result) > query.limit,
                "is_business_evidence": False,
                "sequence_basis": "recorded_section_array_order_not_wall_clock",
            }

    def _selected(self, connection, owner, query):
        self._project(connection, owner, query.project_id)
        row = self.memory._owned(connection, db.episodes, "episode_id", query.episode_id, owner)
        if row["project_id"] != query.project_id or not self._eligible(
            connection, owner, row, query
        ):
            raise AccessDenied("history_outside_current_selection")
        return row

    def window(self, owner, query):
        with self.catalog.transaction() as connection:
            row = self._selected(connection, owner, query)
            events = self._event_rows(connection, row)
            position = next(
                (i for i, e in enumerate(events) if e["event_id"] == query.event_id), None
            )
            if position is None:
                raise AccessDenied("history_anchor_outside_episode")
            return {
                "episode_id": row["episode_id"],
                "anchor": query.event_id,
                "events": events[max(0, position - query.radius) : position + query.radius + 1],
                "is_business_evidence": False,
                "sequence_basis": "recorded_section_array_order_not_wall_clock",
            }

    def source_page(self, owner, query):
        with self.catalog.transaction() as connection:
            row = self._selected(connection, owner, query)
            body = self._body(row)
            _, manifest = self.catalog._available(connection, body["release_id"])
            revision = checked_revision(connection, body["revision_id"])
            for ref in body["reports"]:
                store = ArtifactStore(Path(ref["artifact_root"]))
                report = store.read_json(ref["report_artifact"]["sha256"])
                card = next(
                    (c for c in report["cards"] if c["evidence_id"] == query.evidence_id), None
                )
                if card is None:
                    continue
                # Historical bindings are closed. Replay the original fixed bytes
                # using a read-only context constructed from the saved task spec.
                context = self._historical_context(connection, revision, manifest)
                verification = verify_cards(
                    context, store, [EvidenceCard.model_validate(c) for c in report["cards"]]
                )
                text = card["payload"].get("text")
                if not isinstance(text, str):
                    import json

                    text = json.dumps(card["payload"], ensure_ascii=False, sort_keys=True)
                return {
                    "episode_id": row["episode_id"],
                    "evidence_id": query.evidence_id,
                    "original_release_id": body["release_id"],
                    "source_ids": card["source_ids"],
                    "verification": verification,
                    "historical_source_only": True,
                    **text_page(redact(text), query.offset, query.page_size),
                }
            raise AccessDenied("history_evidence_outside_registered_report")

    def _historical_context(self, connection, revision, manifest):
        # FrozenContext enforces the same release task/source/time selection; its
        # constructor can read closed task metadata through a narrow local adapter.
        from contextlib import contextmanager
        from types import SimpleNamespace

        from database.schema import release_tasks

        task = (
            connection.execute(
                sa.select(release_tasks).where(
                    release_tasks.c.task_id == revision["binding_task_id"]
                )
            )
            .mappings()
            .one()
        )
        plan = Plan.model_validate(revision["body"])
        if (
            content_hash(task["spec"]) != task["spec_sha256"]
            or task["release_id"] != plan.release_id
            or task["spec"]["scope"] != plan.scope.model_dump(mode="json")
            or task["spec"]["allowed_source_ids"] != plan.allowed_source_ids
        ):
            raise ArtifactIntegrityError("historical_binding_spec_changed")
        self.catalog.verify_database(connection, manifest)

        @contextmanager
        def transaction(**kwargs):
            yield connection

        def fixed_task(conn, tid, expected=None):
            if tid != task["task_id"] or expected not in {None, manifest["release_id"]}:
                raise AccessDenied("historical_replay_binding_changed")
            return dict(task), manifest

        catalog = SimpleNamespace(
            store=self.catalog.store,
            transaction=transaction,
            engine=self.catalog.engine,
            task=fixed_task,
        )
        service = self.control.service.fork()
        service.catalog = catalog
        service.count = service._count
        service.links = service._links
        return FrozenContext(service, revision["binding_task_id"])

    def change(self, owner, episode_id, value):
        with self.catalog.transaction() as connection:
            row = self.memory._owned(connection, db.episodes, "episode_id", episode_id, owner)
            if row["revision"] != value.expected_revision:
                raise ControlConflict("history_revision_changed")
            self._source(connection, owner, self._body(row))
            connection.execute(
                db.episodes.update()
                .where(db.episodes.c.episode_id == episode_id)
                .values(enabled=value.enabled, revision=row["revision"] + 1)
            )
            self.memory._event(
                connection,
                owner,
                "episode_enabled" if value.enabled else "episode_disabled",
                {"episode_id": episode_id, "revision": row["revision"] + 1},
            )
            return {
                "episode_id": episode_id,
                "revision": row["revision"] + 1,
                "enabled": value.enabled,
            }

    def validate_refs(self, connection, owner, query, refs):
        self._project(connection, owner, query.project_id)
        for ref in refs:
            row = self.memory._owned(
                connection, db.episodes, "episode_id", ref["episode_id"], owner
            )
            if (
                row["revision"] != ref["revision"]
                or row["body_sha256"] != ref["snapshot_sha256"]
                or not self._eligible(connection, owner, row, query)
            ):
                raise AccessDenied("adopted_history_revoked_or_changed")

    def adopt(self, connection, owner, access, entries):
        from control.access import validate

        validate(connection, access, "research", now(connection))
        existing = set(
            connection.scalars(
                sa.select(db.adoptions.c.episode_id).where(
                    db.adoptions.c.execution_id == access.execution_id
                )
            )
        )
        if len(existing | {e["episode_id"] for e in entries}) > 8:
            raise ValueError("history_adoption_limit")
        for e in entries:
            connection.execute(
                insert(db.adoptions)
                .values(
                    execution_id=access.execution_id,
                    episode_id=e["episode_id"],
                    episode_revision=e["revision"],
                    task_id=access.task_id,
                    owner_id=owner,
                    reference={k: e[k] for k in ("episode_id", "revision", "snapshot_sha256")},
                )
                .on_conflict_do_nothing()
            )

    def audit(self, connection, owner, references):
        result = []
        for ref in references:
            row = self.memory._owned(
                connection, db.episodes, "episode_id", ref["episode_id"], owner
            )
            state = (
                "active"
                if row["enabled"] and row["revision"] == ref["revision"]
                else "disabled_or_changed"
            )
            try:
                self._source(connection, owner, self._body(row))
            except AccessDenied:
                state = "authorization_revoked_or_changed"
            except (ArtifactIntegrityError, OSError):
                state = "source_unavailable"
            result.append(ref | {"current_state": state, "historical_reference_only": True})
        return result

"""Memory primary records are authoritative; index/cache hits are always revalidated."""

import json
import re
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from control.access import AccessDenied, ControlConflict
from control.contracts import scope_within
from ingestion.records import content_hash, prepare_import
from memory import schema as db
from memory.contracts import CandidateInput, Proposal, canonical_value, expired
from releases.catalog import ReleaseUnavailable
from runtime.artifacts import ArtifactIntegrityError


def identifier(prefix):
    return prefix + uuid4().hex


def now(connection):
    return connection.scalar(sa.select(sa.func.clock_timestamp()))


def tokens(text):
    return set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.lower()))


class MemoryService:
    def __init__(self, catalog):
        self.catalog = catalog

    def _state(self, connection, owner):
        connection.execute(
            insert(db.states)
            .values(owner_id=owner, generation=1, enabled=True)
            .on_conflict_do_nothing()
        )
        return dict(
            connection.execute(sa.select(db.states).where(db.states.c.owner_id == owner))
            .mappings()
            .one()
        )

    def _project(self, connection, owner, project):
        value = (
            connection.execute(
                sa.select(db.projects).where(
                    db.projects.c.owner_id == owner, db.projects.c.project_id == project
                )
            )
            .mappings()
            .first()
        )
        if value is None:
            raise AccessDenied("memory_project_not_owned")
        return dict(value)

    def check_project(self, owner, selection):
        if selection:
            with self.catalog.transaction() as connection:
                self._project(connection, owner, selection.project_id)

    def _owned(self, connection, table, key, value, owner):
        result = (
            connection.execute(
                sa.select(table).where(table.c[key] == value, table.c.owner_id == owner)
            )
            .mappings()
            .first()
        )
        if result is None:
            raise AccessDenied("memory_record_not_owned")
        return dict(result)

    def _event(self, connection, owner, action, snapshot, memory_id=None):
        connection.execute(
            db.events.insert().values(
                event_id=identifier("ME-"),
                owner_id=owner,
                action=action,
                memory_id=memory_id,
                snapshot=snapshot,
            )
        )

    def _invalidate(self, connection, owner):
        self._state(connection, owner)
        connection.execute(
            db.states.update()
            .where(db.states.c.owner_id == owner)
            .values(generation=db.states.c.generation + 1)
        )
        connection.execute(db.cache.delete().where(db.cache.c.owner_id == owner))

    def project(self, owner, value):
        with self.catalog.transaction() as connection:
            self._state(connection, owner)
            if connection.scalar(
                sa.select(db.projects.c.project_id).where(
                    db.projects.c.owner_id == owner, db.projects.c.project_id == value.project_id
                )
            ):
                raise ControlConflict("memory_project_already_exists")
            connection.execute(
                db.projects.insert().values(
                    owner_id=owner, project_id=value.project_id, name=value.name, enabled=True
                )
            )
            self._invalidate(connection, owner)
            self._event(connection, owner, "project_created", value.model_dump(mode="json"))
        return {"project_id": value.project_id, "enabled": True}

    def settings(self, owner, value=None):
        with self.catalog.transaction() as connection:
            state = self._state(connection, owner)
            if value:
                if state["generation"] != value.expected_generation:
                    raise ControlConflict("memory_generation_changed")
                if value.project_id:
                    self._project(connection, owner, value.project_id)
                    connection.execute(
                        db.projects.update()
                        .where(
                            db.projects.c.owner_id == owner,
                            db.projects.c.project_id == value.project_id,
                        )
                        .values(enabled=value.enabled)
                    )
                else:
                    connection.execute(
                        db.states.update()
                        .where(db.states.c.owner_id == owner)
                        .values(enabled=value.enabled)
                    )
                self._invalidate(connection, owner)
                self._event(connection, owner, "settings_changed", value.model_dump(mode="json"))
                state = self._state(connection, owner)
            return state | {
                "projects": [
                    dict(p)
                    for p in connection.execute(
                        sa.select(db.projects).where(db.projects.c.owner_id == owner)
                    ).mappings()
                ]
            }

    def source(self, owner, value):
        body = value.model_dump(mode="json")
        with self.catalog.transaction() as connection:
            if value.project_id:
                self._project(connection, owner, value.project_id)
            source_id = identifier("MS-")
            connection.execute(
                db.sources.insert().values(
                    source_id=source_id, owner_id=owner, **body, content_sha256=content_hash(body)
                )
            )
            return {
                "source_id": source_id,
                "content_sha256": content_hash(body),
                "origin": value.origin,
            }

    def _source(self, connection, owner, source_id):
        source = self._owned(connection, db.sources, "source_id", source_id, owner)
        if (
            content_hash({k: source[k] for k in ("project_id", "origin", "text")})
            != source["content_sha256"]
        ):
            raise ArtifactIntegrityError("memory_source_changed")
        return source

    def _candidate(self, connection, owner, value, supersedes=None):
        proposal = Proposal.model_validate(value.proposal.model_dump(mode="json"))
        source = self._source(connection, owner, value.source_id)
        if proposal.project_id:
            self._project(connection, owner, proposal.project_id)
        if source["project_id"] not in {None, proposal.project_id}:
            raise AccessDenied("memory_source_scope_widened")
        body = proposal.model_dump(mode="json")
        fingerprint = content_hash([body, value.source_id, source["content_sha256"], supersedes])
        previous = (
            connection.execute(
                sa.select(db.candidates).where(
                    db.candidates.c.owner_id == owner, db.candidates.c.fingerprint == fingerprint
                )
            )
            .mappings()
            .first()
        )
        if previous:
            return dict(previous)
        record = {
            "candidate_id": identifier("MC-"),
            "owner_id": owner,
            "source_id": value.source_id,
            "proposal": body,
            "proposal_sha256": content_hash(body),
            "fingerprint": fingerprint,
            "state": "pending",
            "revision": 1,
            "supersedes": supersedes[0] if supersedes else None,
            "supersedes_revision": supersedes[1] if supersedes else None,
            "review": None,
        }
        connection.execute(db.candidates.insert().values(**record))
        self._event(connection, owner, "candidate_created", record)
        return record

    def candidate(self, owner, value):
        with self.catalog.transaction() as connection:
            return self._candidate(connection, owner, value)

    def candidates(self, owner):
        with self.catalog.transaction() as connection:
            return [
                dict(r)
                for r in connection.execute(
                    sa.select(db.candidates)
                    .where(db.candidates.c.owner_id == owner)
                    .order_by(db.candidates.c.candidate_id)
                ).mappings()
            ]

    def _proposal(self, item):
        if content_hash(item["body"]) != item["body_sha256"]:
            raise ArtifactIntegrityError("memory_body_changed")
        return Proposal.model_validate(item["body"])

    def _domain(self, connection, proposal, verified_releases=None):
        if proposal.kind not in {"rule", "source_pointer"}:
            return {"method": "authenticated_user_confirmation", "domain_truth": False}
        if verified_releases is None:
            verified_releases = {}
        if proposal.release_id not in verified_releases:
            verified_releases[proposal.release_id] = self.catalog._available(
                connection, proposal.release_id
            )[1]
        manifest = verified_releases[proposal.release_id]
        requested = {s.source_id for s in proposal.sources}
        if not requested <= set(manifest["allowed_source_ids"]):
            raise AccessDenied("memory_source_outside_release")
        from memory.policies import verify

        policy_checks = verify(proposal.policy_id) if proposal.policy_id else None
        base = self.catalog.store.directory(manifest)
        for pointer in proposal.sources:
            if pointer.source_version_id:
                found = False
                for sid in manifest["snapshot_ids"]:
                    batch = prepare_import(base / "raw", sid)
                    if batch.snapshot.source.source_id == pointer.source_id:
                        found |= any(
                            r.version is not None
                            and r.version["version_id"] == pointer.source_version_id
                            for r in batch.rows
                        )
                if not found:
                    raise ValueError("memory_source_version_not_in_release")
        return {
            "method": "installed_policy_and_verified_release_pointer/0.1",
            "policy_id": proposal.policy_id,
            "policy_checks": policy_checks,
            "manifest_sha256": content_hash(manifest),
            "domain_truth": False,
        }

    def _expire(self, connection, owner):
        for item in connection.execute(
            sa.select(db.items).where(db.items.c.owner_id == owner, db.items.c.state == "active")
        ).mappings():
            if expired(self._proposal(item), now(connection)):
                self._change(connection, owner, dict(item), "expired", "database_clock_expiry")

    def _change(self, connection, owner, item, state, note):
        revision = item["revision"] + 1
        connection.execute(
            db.items.update()
            .where(db.items.c.memory_id == item["memory_id"])
            .values(state=state, revision=revision)
        )
        connection.execute(db.index.delete().where(db.index.c.memory_id == item["memory_id"]))
        self._invalidate(connection, owner)
        self._event(
            connection,
            owner,
            state,
            item | {"state": state, "revision": revision, "note": note},
            item["memory_id"],
        )
        return item | {"state": state, "revision": revision}

    def review(self, owner, candidate_id, value):
        with self.catalog.transaction() as connection:
            candidate = self._owned(connection, db.candidates, "candidate_id", candidate_id, owner)
            if (
                candidate["revision"] != value.expected_revision
                or candidate["state"] != "pending"
                or candidate["proposal_sha256"] != value.proposal_sha256
            ):
                raise ControlConflict("memory_candidate_changed_or_reviewed")
            if content_hash(candidate["proposal"]) != candidate["proposal_sha256"]:
                raise ArtifactIntegrityError("memory_candidate_changed")
            memory_id = None
            if value.decision == "approve":
                proposal = Proposal.model_validate(candidate["proposal"])
                source = self._source(connection, owner, candidate["source_id"])
                if proposal.kind in {"official_fact", "hypothesis"}:
                    raise ValueError("facts_and_hypotheses_cannot_become_confirmed_memory")
                if source["origin"] != "user_statement":
                    raise ValueError("summary_repetition_is_not_user_confirmation")
                if expired(proposal, now(connection)):
                    raise ValueError("memory_already_expired")
                if proposal.project_id:
                    self._project(connection, owner, proposal.project_id)
                validation = self._domain(connection, proposal)
                self._expire(connection, owner)
                conflicts = []
                for old in connection.execute(
                    sa.select(db.items).where(
                        db.items.c.owner_id == owner, db.items.c.state == "active"
                    )
                ).mappings():
                    p = self._proposal(old)
                    if (p.scope, p.project_id, p.kind, p.semantic_key) == (
                        proposal.scope,
                        proposal.project_id,
                        proposal.kind,
                        proposal.semantic_key,
                    ):
                        conflicts.append(dict(old))
                if candidate["supersedes"]:
                    old = self._owned(
                        connection, db.items, "memory_id", candidate["supersedes"], owner
                    )
                    if (
                        old["revision"] != candidate["supersedes_revision"]
                        or value.replace_memory_id != old["memory_id"]
                    ):
                        raise ControlConflict("memory_edit_target_changed")
                if conflicts and (
                    len(conflicts) != 1 or conflicts[0]["memory_id"] != value.replace_memory_id
                ):
                    raise ControlConflict("memory_conflict_requires_explicit_replacement")
                if value.replace_memory_id:
                    old = self._owned(
                        connection, db.items, "memory_id", value.replace_memory_id, owner
                    )
                    old_p = self._proposal(old)
                    if (old_p.scope, old_p.project_id, old_p.kind, old_p.semantic_key) != (
                        proposal.scope,
                        proposal.project_id,
                        proposal.kind,
                        proposal.semantic_key,
                    ) or old["state"] == "deleted":
                        raise ControlConflict("memory_replacement_slot_differs_or_deleted")
                    self._change(connection, owner, old, "superseded", value.note)
                body = proposal.model_dump(mode="json")
                memory_id = identifier("M-")
                item = {
                    "memory_id": memory_id,
                    "owner_id": owner,
                    "candidate_id": candidate_id,
                    "body": body,
                    "body_sha256": content_hash(body),
                    "state": "active",
                    "revision": 1,
                    "validation": validation,
                }
                connection.execute(db.items.insert().values(**item))
                connection.execute(
                    db.index.insert().values(
                        memory_id=memory_id,
                        owner_id=owner,
                        revision=1,
                        search_text=proposal.semantic_key + " " + canonical_value(proposal),
                    )
                )
                self._invalidate(connection, owner)
                self._event(connection, owner, "promoted", item, memory_id)
            review = value.model_dump(mode="json") | {
                "reviewed_by": owner,
                "at": now(connection).isoformat(),
                "memory_id": memory_id,
            }
            connection.execute(
                db.candidates.update()
                .where(db.candidates.c.candidate_id == candidate_id)
                .values(
                    state="approved" if memory_id else "rejected",
                    revision=candidate["revision"] + 1,
                    review=review,
                )
            )
            self._event(
                connection,
                owner,
                "candidate_reviewed",
                {"candidate_id": candidate_id, "review": review},
            )
            return {
                "candidate_id": candidate_id,
                "memory_id": memory_id,
                "state": "approved" if memory_id else "rejected",
            }

    def item(self, owner, memory_id):
        with self.catalog.transaction() as connection:
            self._expire(connection, owner)
            item = self._owned(connection, db.items, "memory_id", memory_id, owner)
            self._proposal(item)
            return item

    def change(self, owner, memory_id, value):
        with self.catalog.transaction() as connection:
            item = self._owned(connection, db.items, "memory_id", memory_id, owner)
            if item["revision"] != value.expected_revision or item["state"] == "deleted":
                raise ControlConflict("memory_revision_changed_or_deleted")
            return self._change(
                connection,
                owner,
                item,
                "disabled" if value.action == "disable" else "deleted",
                value.note,
            )

    def edit(self, owner, memory_id, value):
        with self.catalog.transaction() as connection:
            item = self._owned(connection, db.items, "memory_id", memory_id, owner)
            if item["revision"] != value.expected_revision or item["state"] == "deleted":
                raise ControlConflict("memory_revision_changed_or_deleted")
            old, proposed = self._proposal(item), value.proposal
            if (old.scope, old.project_id, old.kind, old.semantic_key) != (
                proposed.scope,
                proposed.project_id,
                proposed.kind,
                proposed.semantic_key,
            ):
                raise ValueError("edit_cannot_change_memory_slot")
            return self._candidate(connection, owner, value, [memory_id, item["revision"]])

    def _eligible(
        self, connection, owner, item, request, *, verify_domain=True, verified_releases=None
    ):
        proposal = self._proposal(item)
        if item["state"] != "active" or expired(proposal, now(connection)):
            return False
        if proposal.project_id not in {None, request.project_id} or not scope_within(
            request.scope, proposal.applies_to
        ):
            return False
        if proposal.release_id and (
            proposal.release_id != request.release_id
            or not {s.source_id for s in proposal.sources} <= set(request.allowed_source_ids)
        ):
            return False
        candidate = self._owned(
            connection, db.candidates, "candidate_id", item["candidate_id"], owner
        )
        if (
            candidate["state"] != "approved"
            or candidate["proposal_sha256"] != item["body_sha256"]
            or (candidate["review"] or {}).get("memory_id") != item["memory_id"]
        ):
            raise ArtifactIntegrityError("memory_review_proof_changed")
        self._source(connection, owner, candidate["source_id"])
        if verify_domain and proposal.kind in {"rule", "source_pointer"}:
            try:
                if self._domain(connection, proposal, verified_releases) != item["validation"]:
                    return False
            except ReleaseUnavailable:
                return False
        return True

    def search(self, owner, request):
        with self.catalog.transaction() as connection:
            project = self._project(connection, owner, request.project_id)
            self._expire(connection, owner)
            state = self._state(connection, owner)
            if not state["enabled"] or not project["enabled"]:
                return {
                    "entries": [],
                    "enabled": False,
                    "generation": state["generation"],
                    "cache_hit": False,
                }
            key = content_hash([owner, state["generation"], request.model_dump(mode="json")])
            cached = (
                connection.execute(
                    sa.select(db.cache).where(
                        db.cache.c.cache_key == key, db.cache.c.owner_id == owner
                    )
                )
                .mappings()
                .first()
            )
            entries = []
            verified_releases: dict = {}
            refs = cached["references"] if cached else None
            # Ownership/project/version filters precede relevance selection. The
            # derived index cannot create authority or resurrect a stale primary row.
            rows = connection.execute(
                sa.select(db.items)
                .join(
                    db.index,
                    sa.and_(
                        db.items.c.memory_id == db.index.c.memory_id,
                        db.items.c.revision == db.index.c.revision,
                    ),
                )
                .where(db.items.c.owner_id == owner, db.index.c.owner_id == owner)
            ).mappings()
            for item in rows:
                if refs is not None and not any(
                    r["memory_id"] == item["memory_id"] and r["revision"] == item["revision"]
                    for r in refs
                ):
                    continue
                if self._eligible(
                    connection, owner, item, request, verified_releases=verified_releases
                ):
                    proposal = self._proposal(item)
                    entries.append(
                        {
                            "memory_id": item["memory_id"],
                            "revision": item["revision"],
                            "body_sha256": item["body_sha256"],
                            "kind": proposal.kind,
                            "semantic_key": proposal.semantic_key,
                            "value": canonical_value(proposal),
                            "scope": proposal.scope,
                            "project_id": proposal.project_id,
                            "release_id": proposal.release_id,
                            "sources": [s.model_dump(mode="json") for s in proposal.sources],
                            "is_business_evidence": False,
                            "cannot_change_authorization": True,
                        }
                    )
            # An explicit project preference overrides the user's general value
            # for the same typed semantic slot, before ranking or limiting.
            slots = {}
            for entry in entries:
                slot = (entry["kind"], entry["semantic_key"])
                if slot not in slots or entry["scope"] == "project":
                    slots[slot] = entry
            entries = list(slots.values())
            entries.sort(
                key=lambda e: (
                    -len(tokens(request.query) & tokens(e["semantic_key"] + " " + e["value"])),
                    e["memory_id"],
                )
            )
            entries = entries[: request.limit]
            if cached is None:
                connection.execute(
                    db.cache.insert().values(
                        cache_key=key,
                        owner_id=owner,
                        generation=state["generation"],
                        references=[
                            {"memory_id": e["memory_id"], "revision": e["revision"]}
                            for e in entries
                        ],
                    )
                )
            return {
                "entries": entries,
                "generation": state["generation"],
                "enabled": True,
                "cache_hit": cached is not None,
            }

    def validate_references(self, connection, owner, request, references, *, verify_domain=True):
        state, project = (
            self._state(connection, owner),
            self._project(connection, owner, request.project_id),
        )
        if references and (not state["enabled"] or not project["enabled"]):
            raise AccessDenied("memory_disabled_during_execution")
        verified_releases: dict = {}
        for reference in references:
            item = self._owned(connection, db.items, "memory_id", reference["memory_id"], owner)
            if (
                item["revision"] != reference["revision"]
                or item["body_sha256"] != reference["body_sha256"]
                or not self._eligible(
                    connection,
                    owner,
                    item,
                    request,
                    verify_domain=verify_domain,
                    verified_releases=verified_releases,
                )
            ):
                raise AccessDenied("adopted_memory_revoked_or_changed")

    def audit(self, connection, owner, references):
        result = []
        for ref in references:
            item = self._owned(connection, db.items, "memory_id", ref["memory_id"], owner)
            proposal = self._proposal(item)
            state = "expired" if expired(proposal, now(connection)) else item["state"]
            result.append(
                ref
                | {
                    "current_state": state,
                    "current_revision": item["revision"],
                    "historical_reference_only": True,
                }
            )
        return result

    def history(self, owner, memory_id):
        with self.catalog.transaction() as connection:
            self._owned(connection, db.items, "memory_id", memory_id, owner)
            return [
                dict(r)
                for r in connection.execute(
                    sa.select(db.events)
                    .where(db.events.c.owner_id == owner, db.events.c.memory_id == memory_id)
                    .order_by(db.events.c.created_at, db.events.c.event_id)
                ).mappings()
            ]

    def enqueue(self, owner, source_id):
        with self.catalog.transaction() as connection:
            self._source(connection, owner, source_id)
            existing = (
                connection.execute(
                    sa.select(db.extractions).where(
                        db.extractions.c.owner_id == owner, db.extractions.c.source_id == source_id
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return dict(existing)
            value = {
                "job_id": identifier("MX-"),
                "owner_id": owner,
                "source_id": source_id,
                "state": "queued",
                "candidate_ids": [],
                "error_code": None,
            }
            connection.execute(db.extractions.insert().values(**value))
            return value

    def extraction(self, owner, job_id):
        with self.catalog.transaction() as connection:
            return self._owned(connection, db.extractions, "job_id", job_id, owner)

    def extract_once(self):
        # All work is deterministic and inside one bounded DB transaction: a crash
        # rolls back candidates + job together. There is no external model request.
        with self.catalog.transaction() as connection:
            job = (
                connection.execute(
                    sa.select(db.extractions)
                    .where(db.extractions.c.state == "queued")
                    .order_by(db.extractions.c.job_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            source = self._source(connection, job["owner_id"], job["source_id"])
            try:
                raw = json.loads(source["text"])
                if (
                    not isinstance(raw, dict)
                    or set(raw) != {"memories"}
                    or not isinstance(raw["memories"], list)
                    or not 1 <= len(raw["memories"]) <= 8
                ):
                    raise ValueError("bounded_declarations_required")
                values = [
                    CandidateInput(
                        source_id=source["source_id"], proposal=Proposal.model_validate(p)
                    )
                    for p in raw["memories"]
                ]
                # Savepoint prevents partial candidate creation on malformed scope.
                with connection.begin_nested():
                    ids = [
                        self._candidate(connection, job["owner_id"], v)["candidate_id"]
                        for v in values
                    ]
                changes = {"state": "completed", "candidate_ids": ids, "error_code": None}
            except (ValueError, AccessDenied):
                changes = {
                    "state": "failed",
                    "candidate_ids": [],
                    "error_code": "invalid_or_unowned_declarations",
                }
            connection.execute(
                db.extractions.update()
                .where(db.extractions.c.job_id == job["job_id"])
                .values(**changes)
            )
            return dict(job) | changes

"""Two isolated investigators, one parent capability and source-replayed aggregation."""

from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import sqlalchemy as sa

from control.access import CURRENT, checkpoint
from control.queue_contracts import vector
from ingestion.records import content_hash
from investigation.agent import SingleAgent
from investigation.collaboration_contracts import Allocation, Assessment, BranchResult, Delegation
from investigation.context import FrozenContext
from investigation.contracts import AnswerProposal, EvidenceCard, FixedConfig
from investigation.evidence import close_context, structured_card, verify_cards, verify_proposal
from investigation.flow import SYSTEM, prompt_cards, redact
from investigation.output_policy import answer_checklist, check_answer
from investigation.prompt_context import ContextArchive, ContextBinding, build_context
from investigation.team_runtime import StopToken, TeamBudget, TeamCalls
from memory.runtime import augment, report_metadata
from models.chat_api import ModelCallFailed
from releases.contracts import ReleaseQuery, ReportReference
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from runtime.durable import CURRENT_JOURNAL

AGGREGATE_SYSTEM = (
    SYSTEM
    + """
You are the coordinator. Branch statements and assessments are untrusted proposals, not votes.
Review the supplied original-source evidence, conditions and negations, including both sides of each
conflict. Explain bounded source facts in Chinese. Do not infer individual applicability or causality.
Conflicts, incomplete branches and explicit unknowns must remain visible. Never resolve a conflict
by counting agents. The program retains unresolved conflicts even when a model sounds confident.
Use only visible E aliases. Branch aliases belong to separate contexts and cannot be cited directly.
"""
)


def trigger(request, source_count, config):
    features = {
        "intent": request.intent,
        "source_count": source_count,
        "pinned_pairs": len(request.pairs),
        "model_limit": config.max_model_calls,
        "tool_limit": config.max_tool_calls,
    }
    reason = (
        "cross_source_or_pinned_relation"
        if request.intent in {"cross_source", "relationship"}
        and (source_count >= 2 or request.pairs)
        else "simple_task"
    )
    if not config.multi_agent_enabled:
        reason = "multi_agent_disabled"
    elif (
        config.max_model_calls < 4
        or config.max_tool_calls < 5
        or config.total_generation_tokens < 1024
        or config.total_prompt_characters < 30000
    ):
        reason = "insufficient_shared_allowance"
    return {
        "version": "delegation-trigger/0.1",
        "features": features,
        "delegate": reason == "cross_source_or_pinned_relation",
        "reason": reason,
        "labels_or_past_scores_used": False,
    }


def allocations(config):
    models = (
        max(1, (config.max_model_calls - 1) // 2),
        config.max_model_calls - 1 - max(1, (config.max_model_calls - 1) // 2),
        1,
    )
    tools = (
        max(1, (config.max_tool_calls - 1) // 2),
        config.max_tool_calls - 1 - max(1, (config.max_tool_calls - 1) // 2),
        1,
    )
    inputs = (
        config.total_prompt_characters // 6,
        config.total_prompt_characters // 2,
        config.total_prompt_characters
        - config.total_prompt_characters // 6
        - config.total_prompt_characters // 2,
    )
    outputs = (
        config.total_generation_tokens // 4,
        config.total_generation_tokens * 3 // 8,
        config.total_generation_tokens
        - config.total_generation_tokens // 4
        - config.total_generation_tokens * 3 // 8,
    )
    return {
        role: Allocation(
            model_calls=models[i],
            tool_calls=tools[i],
            prompt_characters=inputs[i],
            completion_tokens=outputs[i],
        )
        for i, role in enumerate(("data", "document", "coordinator"))
    }


def conflicts(results):
    latest = {}
    for result in results:
        for assessment in result.assessments:
            latest[(result.delegation_id, assessment.issue, assessment.subject)] = assessment
    found = []
    keys = {(issue, subject) for _, issue, subject in latest}
    for issue, subject in sorted(keys):
        entries = [(role, x) for (role, i, s), x in latest.items() if (i, s) == (issue, subject)]
        if {x.value for _, x in entries} >= {"supported", "contradicted"}:
            found.append(
                {
                    "issue": issue,
                    "subject": subject,
                    "status": "unresolved_after_source_replay",
                    "proposals": [
                        {"role": role, **x.model_dump(mode="json")} for role, x in entries
                    ],
                    "resolution_rule": "original_sources_and_conditions_not_majority_vote",
                }
            )
    return found


class CooperativeInvestigator:
    def __init__(
        self, service, model, root: Path, config: FixedConfig | None = None, *, plan_snapshot=None
    ):
        self.service, self.model, self.root = service, model, root
        self.config = config or FixedConfig(
            max_model_calls=8, max_tool_calls=10, context_mode="structured"
        )
        self.plan_snapshot = plan_snapshot or {}

    def run(self, request):
        checkpoint(self.service.catalog, request.task_id, "generate")
        context = FrozenContext(self.service, request.task_id, request.expected_release_id)
        policy = trigger(request, len(context.sources), self.config)
        if not policy["delegate"]:
            result = SingleAgent(
                self.service, self.model, self.root, self.config, plan_snapshot=self.plan_snapshot
            ).run(request)
            result |= {
                "requested_method": "multi_agent",
                "delegation_policy": policy,
                "dependency_graph": [],
            }
            store = ArtifactStore(Path(result["artifact_root"]))
            ref = store.put_json(
                {k: v for k, v in result.items() if k not in {"artifact_root", "report_artifact"}}
            )
            self.service.catalog.reference(
                ReportReference(
                    reference_id="FALLBACK-" + result["run_id"],
                    task_id=request.task_id,
                    artifact_sha256=ref["sha256"],
                )
            )
            return result | {"report_artifact": ref}
        access = CURRENT.get()
        if access is None or access.grant_id is None:
            raise ValueError("multi_agent_requires_parent_execution_capability")
        started = perf_counter()
        run_id = "TEAM-" + uuid4().hex
        store = ArtifactStore(self.root / run_id)
        archive = ContextArchive(store)
        budget = TeamBudget(self.config, self.model.identity["is_simulated"])
        shares = allocations(self.config)
        durable = CURRENT_JOURNAL.get()
        parent_calls = TeamCalls(budget, self.model, "coordinator")
        query = ReleaseQuery(
            task_id=request.task_id,
            expected_release_id=context.manifest["release_id"],
            query=request.retrieval_query,
            method=self.config.retrieval_method,
            top_k=self.config.retrieve_k,
        )
        report = {
            "schema_version": "cooperative-investigation/0.1",
            "method": "multi_agent",
            "run_id": run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "request": request.model_dump(mode="json"),
            "release_id": context.manifest["release_id"],
            "manifest_sha256": context.task["manifest_sha256"],
            "task_spec_sha256": context.task["spec_sha256"],
            "scope": context.scope.model_dump(mode="json"),
            "time": context.time,
            "allowed_source_ids": context.sources,
            "model_identity": self.model.identity,
            "config": self.config.model_dump(mode="json"),
            "generation_mode": "simulated_model"
            if self.model.identity["is_simulated"]
            else "real_api",
            "prompt_version": "coordinator-source-replay/0.1",
            "execution_status": "running",
            "quality_status": "pending_semantic_review",
            "semantic_review_status": "pending",
            "proposal": None,
            "answer": None,
            "error_code": None,
            "cards": [],
            "aliases": {},
            "delegation_policy": policy,
            "dependency_graph": [
                {"node": "data", "depends_on": ["parent_authorization"]},
                {"node": "document", "depends_on": ["parent_authorization"]},
                {
                    "node": "coordinator",
                    "depends_on": ["data.result_or_failure", "document.result_or_failure"],
                },
            ],
            "delegations": [],
            "branch_results": [],
            "conflicts": [],
            "context_builds": [],
            "context_artifacts": {},
        }
        cards = []
        branch_records = {}
        results = []
        tokens = {}
        futures = {}
        branch_timing = {}
        executor = None
        try:
            output = parent_calls.tool(
                "lookup", {"request": query.model_dump(mode="json")}, context.lookup
            )
            origin = store.put_json(
                {
                    "tool": "lookup",
                    "input": {"request": query.model_dump(mode="json")},
                    "output": output,
                }
            )
            cards.append(structured_card(context, origin["sha256"], output, "structured_lookup"))
            delegates = []
            for role in ("data", "document"):
                d = Delegation(
                    delegation_id="DATA" if role == "data" else "DOCUMENT",
                    role=role,
                    parent_task_id=access.task_id,
                    revision_id=access.revision_id,
                    authorization_id=access.grant_id,
                    epoch=access.epoch,
                    release_id=context.manifest["release_id"],
                    manifest_sha256=context.task["manifest_sha256"],
                    goal=(
                        "核查全集范围、结构化统计和已固定的关系；保留配置和个车适用性的未知。"
                        if role == "data"
                        else "检索并展开允许来源原文，核查缺陷、条件、否定、结案及后续监测。"
                    ),
                    scope=context.scope,
                    time=context.time,
                    allowed_source_ids=context.sources,
                    input_evidence_ids=[cards[0].evidence_id],
                    allowed_tools=[
                        "lookup",
                        "count",
                        "links",
                        "read",
                        "finish",
                        "history_search",
                        "history_window",
                    ]
                    if role == "data"
                    else [
                        "lookup",
                        "search",
                        "expand",
                        "read",
                        "finish",
                        "history_search",
                        "history_window",
                    ],
                    allocation=shares[role],
                    completion_conditions=[
                        "只返回有引用的边界结论",
                        "保存反证、未知和失败",
                        "不得扩大父授权",
                    ],
                )
                delegates.append(d)
            report["delegations"] = [d.model_dump(mode="json") for d in delegates]
            executor = ThreadPoolExecutor(max_workers=2)

            def run_branch(d, journal, token):
                import threading

                began = perf_counter()
                with budget.lock:
                    branch_timing[d.role] = {
                        "started_ms": (began - started) * 1000,
                        "thread_id": threading.get_ident(),
                    }
                current = TeamCalls(budget, journal or self.model, d.role, token)
                marker = CURRENT_JOURNAL.set(current)
                a = d.allocation
                config = self.config.model_copy(
                    update={
                        "max_model_calls": a.model_calls,
                        "max_tool_calls": a.tool_calls,
                        "total_prompt_characters": a.prompt_characters,
                        "total_generation_tokens": a.completion_tokens,
                        "generation_tokens": max(
                            256,
                            min(
                                self.config.generation_tokens,
                                1000,
                                a.completion_tokens // a.model_calls,
                            ),
                        ),
                        "context_mode": "structured",
                        "expand_k": min(self.config.expand_k, max(1, a.tool_calls - 2)),
                    }
                )
                service = self.service.fork() if d.role == "data" else self.service
                try:
                    return SingleAgent(
                        service,
                        current,
                        self.root,
                        config,
                        delegation=d,
                        stop_check=token.check,
                        plan_snapshot={"goal": d.goal, "hypotheses": [], "steps": []},
                        seed_evidence=[cards[0]],
                        seed_store=store,
                    ).run(request)
                finally:
                    CURRENT_JOURNAL.reset(marker)
                    with budget.lock:
                        branch_timing[d.role] |= {
                            "finished_ms": (perf_counter() - started) * 1000,
                            "elapsed_ms": (perf_counter() - began) * 1000,
                        }

            for i, d in enumerate(delegates, 1):
                token = StopToken(min(self.config.branch_timeout_seconds, self.config.wall_seconds))
                tokens[d.role] = token
                journal = (
                    durable.fork(
                        i, allocation=vector(**d.allocation.model_dump()), gate_callback=token.check
                    )
                    if durable
                    else None
                )
                futures[d.role] = executor.submit(copy_context().run, run_branch, d, journal, token)
            done, _ = wait(
                futures.values(),
                timeout=min(self.config.branch_timeout_seconds, self.config.wall_seconds),
            )
            for d in delegates:
                future = futures[d.role]
                if future not in done:
                    tokens[d.role].cancelled.set()
                    results.append(
                        BranchResult(
                            delegation_id=d.delegation_id,
                            status="timeout",
                            error_code="branch_deadline_exceeded",
                        )
                    )
                    if durable:
                        self._quarantine_lane(durable, d.role)
                    continue
                try:
                    branch = future.result()
                except Exception as error:
                    import traceback

                    diagnostics = store.put_json(
                        {
                            "role": d.role,
                            "error_type": type(error).__name__,
                            "traceback": traceback.format_exc(),
                        }
                    )
                    report.setdefault("branch_failure_artifacts", {})[d.role] = diagnostics
                    results.append(
                        BranchResult(
                            delegation_id=d.delegation_id,
                            status="failed",
                            error_code=getattr(error, "code", type(error).__name__),
                        )
                    )
                    continue
                branch_records[d.delegation_id] = branch
                branch_store = ArtifactStore(Path(branch["artifact_root"]))
                saved = branch_store.read_json(branch["report_artifact"]["sha256"])
                if (
                    saved["release_id"] != context.manifest["release_id"]
                    or saved["scope"] != report["scope"]
                ):
                    raise ArtifactIntegrityError("delegated_report_scope_changed")
                for raw in saved["cards"]:
                    card = EvidenceCard.model_validate(raw)
                    ref = store.put_json(branch_store.read_json(card.origin_sha256))
                    if ref["sha256"] != card.origin_sha256:
                        raise ArtifactIntegrityError("delegated_origin_changed")
                    if card.evidence_id not in {c.evidence_id for c in cards}:
                        cards.append(card)
                proposal = saved.get("proposal") or {}
                result = BranchResult(
                    delegation_id=d.delegation_id,
                    status="completed" if saved["execution_status"] == "completed" else "failed",
                    disposition=proposal.get("disposition"),
                    report_artifact=branch["report_artifact"],
                    evidence_ids=[c["evidence_id"] for c in saved["cards"]],
                    counterevidence_ids=[
                        saved["aliases"][a]
                        for a in saved.get("recorded_counterevidence_aliases", [])
                    ],
                    assessments=[
                        Assessment.model_validate(a) for a in saved.get("assessments", [])
                    ],
                    unresolved_fields=proposal.get("unresolved_fields", []),
                    error_code=saved.get("error_code"),
                )
                results.append(result)
            executor.shutdown(wait=False, cancel_futures=True)
            report["branch_results"] = [r.model_dump(mode="json") for r in results]
            report["conflicts"] = conflicts(results)
            report["evidence_verification"] = verify_cards(context, store, cards)
            aliases, visible = prompt_cards(cards)
            inverse = {v: k for k, v in aliases.items()}
            for conflict in report["conflicts"]:
                for proposed in conflict["proposals"]:
                    branch = branch_records[proposed["role"]]
                    for field in ("support_ids", "counter_ids"):
                        proposed[field] = [inverse[branch["aliases"][a]] for a in proposed[field]]
            counters = {inverse[e] for r in results for e in r.counterevidence_ids}
            if report["conflicts"]:
                for r in results:
                    for a in r.assessments:
                        counters.update(
                            inverse[branch_records[r.delegation_id]["aliases"][x]]
                            for x in a.support_ids + a.counter_ids
                        )
            summaries = []
            for r in results:
                branch = branch_records.get(r.delegation_id)
                # Archive the stable semantic branch result for the coordinator. Runtime
                # IDs/timings in the private report change after recovery, while its
                # evidence and proposals do not. Keep the full report pointer in results.
                summary = r.model_dump(mode="json", exclude={"report_artifact"})
                handle, _ = archive.add(summary, "branch_" + r.delegation_id)
                summaries.append(
                    {
                        "role": r.delegation_id,
                        "status": r.status,
                        "disposition": r.disposition,
                        "unresolved_fields": r.unresolved_fields,
                        "report_handle": handle,
                        "claims_are_proposals": True,
                        "assessments": [
                            {
                                **a.model_dump(mode="json"),
                                "support_ids": [
                                    inverse[branch["aliases"][e]] for e in a.support_ids
                                ],
                                "counter_ids": [
                                    inverse[branch["aliases"][e]] for e in a.counter_ids
                                ],
                            }
                            for a in r.assessments
                        ]
                        if branch
                        else [],
                        "claims": [
                            c["statement"]
                            for c in ((branch or {}).get("proposal") or {}).get("claims", [])
                        ],
                    }
                )
            payload = {
                "question": redact(request.question),
                "intent": request.intent,
                "scope": report["scope"],
                "time": report["time"],
                "initial_plan": self.plan_snapshot,
                "authorization_limits": {"allowed_source_ids": context.sources},
                "evidence": visible,
                "answer_checklist": answer_checklist(request.question, cards, aliases),
                "candidates": [],
                "observations": [],
                "previous_decisions": [],
                "branch_summaries": summaries,
                "conflicts": report["conflicts"],
            }
            binding = ContextBinding(
                task_id=request.task_id,
                release_id=context.manifest["release_id"],
                task_spec_sha256=context.task["spec_sha256"],
                plan_sha256=content_hash(self.plan_snapshot),
                revision_id=access.revision_id,
                authorization_id=access.grant_id,
                epoch=access.epoch,
            )
            payload["authorization_limits"]["binding"] = binding.model_dump(mode="json")
            system, payload = augment(AGGREGATE_SYSTEM, payload)
            payload, build = build_context(
                system,
                payload,
                cards,
                binding,
                counters,
                archive,
                mode="structured",
                cap=min(self.config.prompt_characters, shares["coordinator"].prompt_characters),
            )
            report["context_builds"].append(build)
            report["input_artifact"] = store.put_json({"system": system, "payload": payload})
            checkpoint(self.service.catalog, request.task_id, "generate")
            response = parent_calls.generate(
                system,
                payload,
                max_tokens=min(
                    self.config.generation_tokens, shares["coordinator"].completion_tokens
                ),
            )
            report["model_output_artifact"] = store.put_json(response)
            checkpoint(self.service.catalog, request.task_id, "generate")
            if not response["accepted_complete"]:
                raise ModelCallFailed("model_completion_truncated_or_refused")
            proposal = AnswerProposal.model_validate(response["proposal"])
            unknowns = [field for r in results for field in r.unresolved_fields]
            unknowns += [
                "individual_vehicle" if a.issue == "applicability" else "source_coverage"
                for r in results
                for a in r.assessments
                if a.value == "unknown"
            ]
            proposal = AnswerProposal.model_validate(
                proposal.model_dump(mode="json")
                | {"unresolved_fields": list(dict.fromkeys(proposal.unresolved_fields + unknowns))}
            )
            if proposal.disposition not in {"answered", "insufficient_evidence"}:
                raise ValueError("model_cannot_override_program_guards")
            proposal, attachments = close_context(proposal, cards, aliases)
            report["context_attachments"] = attachments
            report["claim_verification"] = verify_proposal(proposal, cards, aliases, context.scope)
            report["output_checks"] = check_answer(proposal, cards, aliases, request.question)
            if report["output_checks"]["findings"]:
                raise ModelCallFailed("answer_publication_checks_failed")
            shown = {
                a for e in payload["evidence"] for a in e.get("equivalent_aliases", [e["alias"]])
            }
            used = {a for c in proposal.claims for a in c.support_ids + c.counter_ids}
            if not used <= shown:
                raise ValueError("coordinator_cites_undisclosed_evidence")
            if proposal.disposition == "answered" and not counters <= used:
                raise ValueError("coordinator_dropped_counterevidence")
            partial = any(r.status != "completed" for r in results)
            if report["conflicts"] or partial:
                proposal.disposition = "insufficient_evidence"
                proposal.unresolved_fields = list(
                    dict.fromkeys(
                        proposal.unresolved_fields
                        + [
                            "individual_vehicle"
                            if any(c["issue"] == "applicability" for c in report["conflicts"])
                            else "source_coverage"
                        ]
                    )
                )
            report["proposal"] = proposal.model_dump(mode="json")
            report["answer"] = (
                "\n".join(
                    c.statement + " [" + ",".join(c.support_ids + c.counter_ids) + "]"
                    for c in proposal.claims
                )
                or "当前证据不足。"
            )
            if report["conflicts"]:
                report["answer"] += (
                    "\n分支判断存在未消解冲突；已回查原文，不能据此确认个车适用、因果或普遍安全。"
                )
            if partial:
                report["answer"] += (
                    "\n部分分支失败或超时，已保留成功分支成果，缺失结果不等于没有记录。"
                )
            report["execution_status"] = "completed"
        except Exception as error:
            report.update(
                execution_status="failed",
                error_code=getattr(error, "code", type(error).__name__),
                answer=None,
            )
        finally:
            for role, token in tokens.items():
                if role in futures and not futures[role].done():
                    token.cancelled.set()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        report["cards"] = [c.model_dump(mode="json") for c in cards]
        report["branch_artifact_roots"] = {
            role: branch["artifact_root"] for role, branch in branch_records.items()
        }
        with budget.lock:
            report["branch_timing"] = deepcopy(branch_timing)
        report["seed_evidence_reused_by_both_branches"] = [cards[0].evidence_id] if cards else []
        report["distinct_evidence_keys"] = len({c.evidence_key for c in cards})
        report["aliases"] = prompt_cards(cards)[0]
        report["resources"] = budget.snapshot()
        report["model_calls"] = report["resources"]["model_calls"]
        with budget.lock:
            report["tool_calls"] = deepcopy(budget.tools)
        report["total_elapsed_ms"] = (perf_counter() - started) * 1000
        report["verification_origin_replays"] = (report.get("evidence_verification") or {}).get(
            "origin_replays", 0
        ) + sum(r.get("verification_origin_replays", 0) for r in branch_records.values())
        report["context_artifacts"] = archive.handles
        report.update(report_metadata())
        ref = store.put_json(report)
        checkpoint(self.service.catalog, request.task_id, "generate")
        self.service.catalog.reference(
            ReportReference(
                reference_id=run_id, task_id=request.task_id, artifact_sha256=ref["sha256"]
            )
        )
        return report | {"report_artifact": ref, "artifact_root": (self.root / run_id).as_posix()}

    def _quarantine_lane(self, journal, role):
        from control.access import row, validate
        from control.dispatch import db_now
        from control.queue_contracts import add
        from control.queue_schema import calls, jobs

        lane = 1 if role == "data" else 2
        with journal.dispatcher.catalog.transaction() as connection:
            validate(connection, journal.claim.access, "research", db_now(connection))
            job = row(connection, jobs, "job_id", journal.claim.job_id)
            for call in connection.execute(
                sa.select(calls).where(
                    calls.c.job_id == job["job_id"],
                    calls.c.ordinal >= lane * 100,
                    calls.c.ordinal < (lane + 1) * 100,
                    calls.c.state == "reserved",
                )
            ).mappings():
                pool = journal.dispatcher._pool(connection, job["root_task_id"])
                from control.queue_schema import pools

                connection.execute(
                    pools.update()
                    .where(pools.c.root_task_id == pool["root_task_id"])
                    .values(
                        used=add(pool["used"], call["reserved"]),
                        held=add(pool["held"], call["reserved"], -1),
                    )
                )
                connection.execute(
                    calls.update()
                    .where(calls.c.call_id == call["call_id"])
                    .values(
                        state="unknown",
                        charged=call["reserved"],
                        pending_reconciliation=call["kind"] == "model",
                        error_code="delegated_branch_timeout",
                    )
                )

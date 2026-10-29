"""Audit a frozen confirmation snapshot; missing human components stay missing."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

from pydantic import ValidationError

from evaluation.analysis import paired_interval
from evaluation.freeze import load
from ingestion.records import content_hash
from investigation.agent_contracts import Decision
from investigation.contracts import EvidenceCard
from investigation.evidence import close_context, verify_proposal
from retrieval.contracts import SearchScope
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore
from scripts.freeze_quality_confirmation import wrapper_identity
from scripts.summarize_human_quality_components import bind_atoms


def diagnose_failed(report, store):
    """Recheck saved contracts only; never execute a tool or ask a model to repair."""
    decisions = report.get("decisions") or []
    if not decisions or not decisions[-1].get("output_artifact"):
        return {
            "kind": "unresolved_runtime_or_branch_failure",
            "error_code": report.get("error_code"),
        }
    entry = decisions[-1]
    response = store.read_json(entry["output_artifact"]["sha256"])
    request = store.read_json(entry["input_artifact"]["sha256"])
    try:
        decision = Decision.model_validate(response["proposal"])
        if decision.action not in request["payload"].get("available_tools", []):
            return {"kind": "unavailable_tool_requested", "action": decision.action}
        if decision.action == "finish":
            cards = [EvidenceCard.model_validate(c) for c in report["cards"]]
            proposal, _ = close_context(decision.proposal, cards, report["aliases"])
            verify_proposal(
                proposal, cards, report["aliases"], SearchScope.model_validate(report["scope"])
            )
    except ValidationError as error:
        return {
            "kind": "saved_decision_contract_failure",
            "errors": error.errors(include_input=False, include_url=False, include_context=False),
        }
    except ValueError as error:
        return {"kind": "saved_proposal_constraint_failure", "reason": str(error)}
    return {"kind": "remaining_runtime_cause_unresolved", "last_action": decision.action}


def accounted_output(entry):
    value = (entry.get("usage") or {}).get("completion_tokens")
    return value if type(value) is int and value >= 0 else entry["max_tokens"]


def judgment_origin(row, field):
    return row.get("task_judgment_question_ids", {}).get(field, row["source_question_id"])


def bind_human(source, observations, reports):
    if source["source_kind"] != "actual_human_reply" or source["reviewer_kind"] != "human":
        raise ValueError("confirmation_requires_actual_human_source")
    candidate = read(source["candidate_packet"])
    packet = read(source["source_packet"])
    observed = observations[source["cell_id"]]
    if (
        content_hash(packet) != source["source_packet_sha256"]
        or candidate["packet_sha256"] != source["source_packet_sha256"]
        or packet["blind_id"] != source["blind_id"]
        or observed["blind_id"] != source["blind_id"]
        or content_hash(observed) != source["observation_sha256"]
        or packet.get("proposal") != reports[source["cell_id"]].get("proposal")
        or [c["claim"] for c in candidate["claims"]]
        != (packet.get("proposal") or {}).get("claims", [])
    ):
        raise ValueError("human_confirmation_observation_binding_changed")
    return bind_atoms(
        source,
        {
            "items": [
                {
                    "case_id": source["case_id"],
                    "packet_sha256": source["source_packet_sha256"],
                    "claims": candidate["claims"],
                }
            ]
        },
    ) | {
        "cell_id": source["cell_id"],
        "source_question_id": source["source_question_id"],
        "disposition_appropriate": source["disposition_appropriate"],
        "core_task_complete": source["core_task_complete"],
        "citation_roles": source.get("citation_roles", []),
        "relation_judgments": source.get("relation_judgments"),
    }


def acceptance_from_witnesses(targets, human_rows, stage_rows=(), reading_rows=(), failure_rows=()):
    witnesses = []
    for row in human_rows:
        for atom in row["atoms"]:
            if (
                atom["verdict"] == "unsupported"
                and targets["atomic_unsupported_event_cluster_mean_maximum"] == 0
            ):
                witnesses.append(
                    {
                        "metric": "atomic_unsupported_event_cluster_mean_maximum",
                        "cell_id": row["cell_id"],
                        "atom_id": atom["atom_id"],
                        "quote": atom["quote"],
                        "actual_citations": atom["support_ids"],
                        "basis": atom.get("basis"),
                        "human_source_question_id": row["source_question_id"],
                        "reason": "One verified unsupported atom makes a finite, nonnegative event-cluster mean strictly positive; full rate remains unmeasured.",
                    }
                )
        for citation in row.get("citation_roles", []):
            if (
                citation["role"].startswith("does_not_support")
                and targets.get("named_aspect_citation_support_minimum") == 1
            ):
                witnesses.append(
                    {
                        "metric": "named_aspect_citation_support_minimum",
                        "cell_id": row["cell_id"],
                        "atom_ids": citation["atom_ids"],
                        "actual_citation": citation["alias"],
                        "human_source_question_id": row["source_question_id"],
                        "reason": "Human explicitly found the named aspect absent from its actual citation; missing rates remain null.",
                    }
                )
    stage_groups: dict[tuple[str, str], dict] = {}
    for row in stage_rows:
        if targets.get("stop_stage_appropriateness_minimum") == 1 and (
            row["disposition_verdict"] == "partially_appropriate"
            or any(v == "partially_satisfied" for v in row.get("stage_obligations", {}).values())
        ):
            key = (row["source_question_id"], row["review_id"])
            stage_groups.setdefault(key, {"row": row, "cells": []})["cells"].append(row["cell_id"])
    for group in stage_groups.values():
        row = group["row"]
        witnesses.append(
            {
                "metric": "stop_stage_appropriateness_minimum",
                "cell_ids": group["cells"],
                "review_id": row["review_id"],
                "human_source_question_id": row["source_question_id"],
                "reason": row["reason"],
                "qualification": "Disposition direction was accepted; human found stage explanation/obligation only partially satisfied. No numerical partial score imputed.",
            }
        )
    for row in reading_rows:
        for finding in row.get("unambiguous_adjudicated_findings", []):
            if (
                finding["finding"].startswith("does_not_directly_support")
                and targets.get("named_aspect_citation_support_minimum") == 1
            ):
                witnesses.append(
                    {
                        "metric": "named_aspect_citation_support_minimum",
                        "cell_id": row["cell_id"],
                        "navigation_id": finding.get("navigation_id"),
                        "claim_id": finding.get("claim_id"),
                        "named_aspect": finding.get("named_aspect"),
                        "actual_citations": finding.get("evidence_aliases")
                        or [finding["evidence_alias"]],
                        "human_source_question_id": finding["source_question_id"],
                        "reason": "Actual human recheck found that the actual citation does not directly support this named claim aspect. No implementation-selected atomic category or denominator is inferred.",
                    }
                )
    failed_core = [
        r
        for r in [*failure_rows, *reading_rows, *human_rows]
        if r.get("core_task_complete") is False
    ]
    if failed_core and targets.get("generated_core_event_cluster_mean_minimum") == 1:
        witnesses.append(
            {
                "metric": "generated_core_event_cluster_mean_minimum",
                "cell_ids": [r["cell_id"] for r in failed_core],
                "human_source_question_ids": sorted(
                    {judgment_origin(r, "core_task_complete") for r in failed_core}
                ),
                "reason": "Actual human review marked these tasks not fully core-complete because of failed execution or required-content omissions; a required all-complete event-cluster mean cannot equal one. Full rate remains unmeasured.",
            }
        )
    unacceptable = [r for r in failure_rows if r["disposition_appropriate"] is False]
    if unacceptable and targets.get("disposition_appropriateness_minimum") == 1:
        witnesses.append(
            {
                "metric": "disposition_appropriateness_minimum",
                "cell_ids": [r["cell_id"] for r in unacceptable],
                "human_source_question_ids": sorted(
                    {r["source_question_id"] for r in unacceptable}
                ),
                "reason": "Actual human explicitly rejected failed empty output as an acceptable limited-source disposition; no numerical population rate imputed.",
            }
        )
    return {
        "status": "not_passed" if witnesses else "not_established",
        "verified_target_violations": witnesses,
        "complete_population_semantic_rates": None,
        "passing_acceptance_allowed": False,
        "human_reviews_are_not_imputed": True,
    }


def bind_stage_human(source, observations):
    if source["source_kind"] != "actual_human_reply" or source["reviewer_kind"] != "human":
        raise ValueError("stage_review_requires_actual_human_reply")
    reading = read(source["reading_json"])
    if content_hash(reading) != source["reading_sha256"]:
        raise ValueError("human_stage_reading_changed")
    groups = {g["review_id"]: g for g in reading["groups"]}
    rows = []
    for judgment in source["group_judgments"]:
        group = groups[judgment["review_id"]]
        for cid, digest in zip(group["cell_ids"], group["observation_sha256s"], strict=True):
            if content_hash(observations[cid]) != digest:
                raise ValueError("human_stage_observation_changed")
            rows.append(
                {
                    **judgment,
                    "cell_id": cid,
                    "group_id": group["group_id"],
                    "source_question_id": source["source_question_id"],
                    "human_source_sha256": content_hash(source),
                }
            )
    if len({r["cell_id"] for r in rows}) != len(rows):
        raise ValueError("human_stage_group_repeated")
    return rows


def bind_reading_human(source, observations, reports):
    """A completed reading may still lack a final atomic or relation adjudication."""
    if source["source_kind"] != "actual_human_reply" or source["reviewer_kind"] != "human":
        raise ValueError("reading_requires_actual_human_source")
    packet = read(source["source_packet"])
    observed = observations[source["cell_id"]]
    if (
        content_hash(packet) != source["source_packet_sha256"]
        or content_hash(observed) != source["observation_sha256"]
        or packet["blind_id"] != source["blind_id"]
        or observed["blind_id"] != source["blind_id"]
        or packet.get("proposal") != reports[source["cell_id"]].get("proposal")
    ):
        raise ValueError("human_reading_observation_binding_changed")
    actual_sources = []
    for field in ("initial_review", "adjudication"):
        if field + "_path" not in source:
            if field == "initial_review":
                raise ValueError("human_reading_original_source_missing")
            continue
        recorded = read(source[field + "_path"])
        if (
            content_hash(recorded) != source[field + "_sha256"]
            or recorded["cell_id"] != source["cell_id"]
            or recorded["source_kind"] != "actual_human_reply"
            or recorded["source_question_id"] not in source["source_question_ids"]
        ):
            raise ValueError("human_reading_source_or_adjudication_changed")
        actual_sources.append(recorded)
    if source["complete_task_reading"] is not True:
        raise ValueError("human_complete_reading_not_recorded")
    if source.get("atomic_grades_complete") is not False:
        raise ValueError("claim_reading_cannot_certify_atomic_grades")
    task_origins = {}
    for field in ("disposition_appropriate", "core_task_complete", "all_requirements_verdict"):
        if field not in source:
            continue
        supplied = [r for r in actual_sources if field in r]
        if not supplied or source[field] != supplied[-1][field]:
            raise ValueError("human_reading_task_judgment_not_in_latest_source")
        task_origins[field] = supplied[-1]["source_question_id"]
    return source | {
        "human_source_sha256": content_hash(source),
        "task_judgment_question_ids": task_origins,
    }


def bind_failure_human(source, observations):
    if source["source_kind"] != "actual_human_reply" or source["reviewer_kind"] != "human":
        raise ValueError("failure_review_requires_actual_human_reply")
    reading = read(source["reading_json"])
    if content_hash(reading) != source["reading_sha256"]:
        raise ValueError("failure_reading_changed")
    items = {item["review_id"]: item for item in reading["items"]}
    rows = []
    for judgment in source["item_judgments"]:
        item = items[judgment["review_id"]]
        observation = observations[judgment["cell_id"]]
        if (
            item["cell_id"] != judgment["cell_id"]
            or content_hash(observation) != item["observation_sha256"]
            or judgment["observation_sha256"] != item["observation_sha256"]
            or observation["state"] != "failed"
            or (observation.get("report_summary") or {}).get("proposal") is not None
        ):
            raise ValueError("human_failure_observation_binding_changed")
        rows.append(
            judgment
            | {
                "source_question_id": source["source_question_id"],
                "human_source_sha256": content_hash(source),
            }
        )
    return rows


def generated_metric_coverage(expected, provider_limits, human_rows):
    """Only complete per-arm metric denominators produce a cluster mean."""
    by_cell = {r["cell_id"]: r for r in human_rows}
    result = {}
    non_unqualified = {
        "satisfied_with_minor_scope_caveat",
        "satisfied_with_scope_and_citation_caveats",
        "satisfied_with_minor_caveat",
        "partially_satisfied_with_minor_caveats",
        "not_completed",
        "not_satisfied",
        "not_fully_satisfied_missing_certain_and_citation_misalignment",
        "human_keeps_overall_satisfaction_with_scope_caveat",
    }
    for arm in sorted({c["arm_id"] for c in expected.values()}):
        units = [
            c for cid, c in expected.items() if c["arm_id"] == arm and provider_limits[cid] > 0
        ]
        metrics = {}
        for name in ("core_task_complete", "unqualified_requirements"):
            clusters = defaultdict(list)
            missing = []
            known = []
            for cell in units:
                row = by_cell.get(cell["cell_id"], {})
                value = row.get("core_task_complete")
                if name == "unqualified_requirements":
                    verdict = row.get("all_requirements", row.get("all_requirements_verdict"))
                    value = (
                        True
                        if verdict == "satisfied"
                        else False
                        if verdict in non_unqualified
                        else None
                    )
                if type(value) is not bool:
                    missing.append(cell["cell_id"])
                    continue
                known.append(
                    {
                        "cell_id": cell["cell_id"],
                        "value": value,
                        "human_source_question_id": judgment_origin(
                            row,
                            "core_task_complete"
                            if name == "core_task_complete"
                            else "all_requirements_verdict",
                        ),
                    }
                )
                clusters[cell["event_cluster_id"]].append(int(value))
            complete = not missing and bool(units)
            metrics[name] = {
                "frozen_generated_attempts": len(units),
                "actually_judged": len(known),
                "complete_metric_denominator": complete,
                "missing_cell_ids": missing,
                "event_cluster_mean": mean(mean(v) for v in clusters.values())
                if complete
                else None,
                "judgments": known,
            }
        result[arm] = metrics
    return result


def load_human_index(path, experiment_id):
    index = read(path)
    if index["experiment_id"] != experiment_id:
        raise ValueError("human_source_index_for_another_experiment")
    result = []
    for kind in ("human", "human_stage", "human_reading", "human_failure"):
        paths = []
        for item in index[kind]:
            source = read(item["path"])
            if content_hash(source) != item["content_sha256"]:
                raise ValueError("indexed_human_source_changed")
            paths.append(Path(item["path"]))
        result.append(paths)
    return result


def analyze(identity, root, human_paths, stage_paths=(), reading_paths=(), failure_paths=()):
    body, protocol = load(identity)
    if protocol.quality_targets["new_wrapper_implementation"] != wrapper_identity():
        raise ValueError("frozen_wrapper_changed")
    index = read(root / "runs.json")
    expected = {c["cell_id"]: c for c in body["cells"]}
    if index["experiment_id"] != identity["experiment_id"] or set(index["rows"]) != set(expected):
        raise ValueError("frozen_confirmation_denominator_changed")
    records, provider = ArtifactStore(root), ArtifactStore(root / "provider")
    observations = {}
    reports = {}
    failures = []
    for cid, row in index["rows"].items():
        if any(row[k] != v for k, v in expected[cid].items()):
            raise ValueError("confirmation_cell_changed")
        if not row.get("record"):
            continue
        observed = records.read_json(row["record"]["sha256"])
        if observed["cell"] != expected[cid] or observed["state"] != row["state"]:
            raise ValueError("confirmation_observation_changed")
        observations[cid] = observed
        ref = observed.get("report_reference")
        if ref:
            report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                ref["report_artifact"]["sha256"]
            )
            if content_hash(report) != observed["report_content_sha256"]:
                raise ValueError("confirmation_report_changed")
            reports[cid] = report
            if observed["state"] == "failed":
                failures.append(
                    {
                        "cell_id": cid,
                        "observation_sha256": content_hash(observed),
                        "report_sha256": content_hash(report),
                        "diagnosis": diagnose_failed(
                            report, ArtifactStore(Path(ref["artifact_root"]))
                        ),
                    }
                )
    ledger = read(root / "provider-requests.json")
    spec = ledger["spec"]
    if spec["experiment_id"] != identity["experiment_id"] or set(spec["cells"]) != set(expected):
        raise ValueError("confirmation_provider_identity_changed")
    per_cell = defaultdict(list)
    totals: Counter[str] = Counter()
    unknown_usage = 0
    per_input_characters: Counter[str] = Counter()
    for entry in ledger["entries"]:
        per_cell[entry["cell"]].append(entry)
        usage = entry.get("usage") or {}
        known = all(
            type(usage.get(k)) is int
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        if known:
            totals.update(
                {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            )
        else:
            unknown_usage += 1
        if entry.get("artifact"):
            artifact = provider.read_json(entry["artifact"]["sha256"])
            request = provider.read_json(artifact["input_artifact"]["sha256"])
            if content_hash(request) != entry["fingerprint"]:
                raise ValueError("provider_input_fingerprint_changed")
            per_input_characters[entry["cell"]] += len(request["system"]) + len(
                json.dumps(request["payload"], ensure_ascii=False)
            )
    output_accounted = {
        cid: sum(accounted_output(e) for e in entries) for cid, entries in per_cell.items()
    }
    limits_ok = (
        len(ledger["entries"]) <= spec["maximum_requests"] == body["maximum_provider_requests"]
        and all(len(entries) <= spec["cells"][cid] for cid, entries in per_cell.items())
        and all(n <= protocol.profile.total_generation_tokens for n in output_accounted.values())
        and all(
            n <= protocol.profile.total_prompt_characters for n in per_input_characters.values()
        )
        and all(e["max_tokens"] <= protocol.profile.generation_tokens for e in ledger["entries"])
    )
    human_rows = []
    for path in human_paths:
        source = read(path)
        if source["experiment_id"] != identity["experiment_id"]:
            raise ValueError("human_source_is_for_another_experiment")
        human_rows.append(
            bind_human(source, observations, reports) | {"source_path": path.as_posix()}
        )
    if len({r["cell_id"] for r in human_rows}) != len(human_rows):
        raise ValueError("duplicate_human_confirmation_review")
    stage_rows = []
    for path in stage_paths:
        source = read(path)
        if source["experiment_id"] != identity["experiment_id"]:
            raise ValueError("human_stage_source_is_for_another_experiment")
        stage_rows += bind_stage_human(source, observations)
    if len({r["cell_id"] for r in stage_rows}) != len(stage_rows):
        raise ValueError("duplicate_human_stage_review")
    reading_rows = []
    for path in reading_paths:
        source = read(path)
        if source["experiment_id"] != identity["experiment_id"]:
            raise ValueError("human_reading_is_for_another_experiment")
        reading_rows.append(bind_reading_human(source, observations, reports))
    failure_rows = []
    for path in failure_paths:
        source = read(path)
        if source["experiment_id"] != identity["experiment_id"]:
            raise ValueError("human_failure_is_for_another_experiment")
        failure_rows += bind_failure_human(source, observations)
    all_reviewed_ids = [r["cell_id"] for r in human_rows + stage_rows + reading_rows + failure_rows]
    if len(set(all_reviewed_ids)) != len(all_reviewed_ids):
        raise ValueError("human_cell_repeated_across_review_kinds")
    by_arm = {}
    for arm in protocol.arms:
        units = [cid for cid, cell in expected.items() if cell["arm_id"] == arm.arm_id]
        latencies = [
            observations[cid]["end_to_end_ms"]
            for cid in units
            if cid in observations and observations[cid].get("end_to_end_ms") is not None
        ]
        by_arm[arm.arm_id] = {
            "frozen_units": len(units),
            "states": dict(Counter(index["rows"][cid]["state"] for cid in units)),
            "provider_requests": sum(len(per_cell[cid]) for cid in units),
            "known_output_tokens": sum(
                (e.get("usage") or {}).get("completion_tokens") or 0
                for cid in units
                for e in per_cell[cid]
            ),
            "observed_latency_median_ms": median(latencies) if latencies else None,
            "actual_human_component_reviews": sum(r["cell_id"] in units for r in human_rows),
            "actual_human_stage_reviews": sum(r["cell_id"] in units for r in stage_rows),
            "actual_human_readings_with_pending_components": sum(
                r["cell_id"] in units for r in reading_rows
            ),
            "actual_human_failed_task_reviews": sum(r["cell_id"] in units for r in failure_rows),
            "whole_population_semantic_metrics": None,
        }
    required = set(protocol.quality_targets["mandatory_human_cells"])
    reviewed = set(all_reviewed_ids)
    paired_rows = []
    for cid, cell in expected.items():
        entries = per_cell[cid]
        paired_observation = observations.get(cid)
        paired_rows.append(
            {
                "cell": cell,
                "end_to_end_ms": (paired_observation or {}).get("end_to_end_ms"),
                "provider_requests": len(entries) if paired_observation else None,
                "provider_output_tokens": sum(
                    (e.get("usage") or {})["completion_tokens"] for e in entries
                )
                if paired_observation
                and all(
                    type((e.get("usage") or {}).get("completion_tokens")) is int for e in entries
                )
                else None,
                "formal_task_success": None,
            }
        )
    paired = [
        paired_interval(
            paired_rows,
            "fixed",
            arm,
            metric,
            seed=protocol.random_seed,
            draws=protocol.bootstrap_draws,
            minimum=protocol.minimum_clusters_for_interval,
        )
        for arm in ("single_agent", "parallel_tools", "multi_agent")
        for metric in (
            "end_to_end_ms",
            "provider_requests",
            "provider_output_tokens",
            "formal_task_success",
        )
    ]
    lifecycle = read(root / "lifecycle.json") if (root / "lifecycle.json").exists() else None
    metric_coverage = generated_metric_coverage(
        expected, spec["cells"], [*human_rows, *reading_rows, *failure_rows]
    )
    targets = protocol.quality_targets["prospective_numerical"]
    acceptance = acceptance_from_witnesses(
        targets, human_rows, stage_rows, reading_rows, failure_rows
    )
    diagnostic_comparisons = []
    for arm, metrics in metric_coverage.items():
        for name, target in (
            ("core_task_complete", "generated_core_event_cluster_mean_minimum"),
            (
                "unqualified_requirements",
                "generated_unqualified_requirements_event_cluster_mean_minimum",
            ),
        ):
            value = metrics[name]["event_cluster_mean"]
            if value is not None and value < targets[target]:
                diagnostic_comparisons.append(
                    {
                        "metric": target,
                        "arm_id": arm,
                        "actual_event_cluster_mean": value,
                        "minimum": targets[target],
                        "complete_metric_denominator": metrics[name]["frozen_generated_attempts"],
                        "reason": "This arm has a complete denominator for this individual metric. This diagnostic comparison does not substitute for incomplete pooled semantic measurements or add a post-run acceptance rule.",
                    }
                )
    if acceptance["verified_target_violations"]:
        acceptance["status"] = "not_passed"
    return {
        "schema_version": "quality-confirmation-analysis/0.1",
        "experiment_id": identity["experiment_id"],
        "run_index_sha256": content_hash(index),
        "provider_ledger_sha256": content_hash(ledger),
        "frozen_units": len(expected),
        "observed_units": len(observations),
        "states": dict(Counter(row["state"] for row in index["rows"].values())),
        "partial_snapshot": len(observations) != len(expected),
        "by_arm": by_arm,
        "paired": paired,
        "paired_comparison_limits": "Time and token/request intervals include failures and guards. No semantic benefit inferred from faster failures; five clusters give limited precision and no coverage of old ablation arms.",
        "provider": {
            "requests": len(ledger["entries"]),
            "maximum_requests": spec["maximum_requests"],
            "states": dict(Counter(e["state"] for e in ledger["entries"])),
            "known_usage_subtotal": dict(totals),
            "complete_usage": dict(totals) if unknown_usage == 0 else None,
            "requests_without_complete_usage": unknown_usage,
            "reserved_or_actual_output_tokens": sum(output_accounted.values()),
            "maximum_root_input_characters_observed": max(per_input_characters.values(), default=0),
            "request_and_root_budgets_respected": limits_ok,
            "actual_cost": None,
        },
        "human_rows": human_rows,
        "human_stage_rows": stage_rows,
        "human_readings_pending_components": reading_rows,
        "human_failed_task_reviews": failure_rows,
        "generated_metric_coverage": metric_coverage,
        "per_arm_diagnostic_target_comparisons": diagnostic_comparisons,
        "failed_execution_contract_rechecks": failures,
        "mandatory_human_coverage": {
            "predeclared": len(required),
            "component_reviews_completed": len(required & {r["cell_id"] for r in human_rows}),
            "stage_reviews_completed": len(required & {r["cell_id"] for r in stage_rows}),
            "readings_pending_component_adjudication": len(
                required & {r["cell_id"] for r in reading_rows}
            ),
            "failed_task_reviews_completed": len(required & {r["cell_id"] for r in failure_rows}),
            "any_actual_human_review_completed": len(required & reviewed),
            "remaining_cell_ids": sorted(required - reviewed),
            "stage_reviews_counted_here": True,
        },
        "acceptance": acceptance,
        "lifecycle": lifecycle,
        "new_provider_calls_by_analysis": 0,
        "limitations": [
            "Subset human rates never replace whole-population semantic rates.",
            "Citation-local lack of support does not establish factual falsity in the source corpus.",
            "All frozen failures and unstarted units remain in the denominator.",
            "Old retrieval/context/memory/recovery/interference semantic gaps remain separately open.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--human", type=Path, action="append", default=[])
    parser.add_argument("--human-stage", type=Path, action="append", default=[])
    parser.add_argument("--human-reading", type=Path, action="append", default=[])
    parser.add_argument("--human-failure", type=Path, action="append", default=[])
    parser.add_argument("--human-index", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    identity = read(args.experiment)
    sources = [args.human, args.human_stage, args.human_reading, args.human_failure]
    if args.human_index:
        if any(sources):
            raise ValueError("choose_human_index_or_explicit_sources")
        sources = load_human_index(args.human_index, identity["experiment_id"])
    result = analyze(identity, args.root, *sources)
    if args.human_index:
        result["human_source_index_sha256"] = content_hash(read(args.human_index))
    write_new(args.output, result)
    print({k: result[k] for k in ("states", "observed_units", "provider", "acceptance")})


if __name__ == "__main__":
    main()

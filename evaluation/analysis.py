"""Clustered paired summaries keep failed, unrun and unreviewed tasks visible."""

from collections import Counter, defaultdict
from random import Random
from statistics import mean, median

from evaluation.contracts import cells


def paired_interval(rows, left, right, metric, *, seed=20261028, draws=2000, minimum=5):
    selected = {
        arm: {
            (r["cell"]["case_id"], r["cell"]["repeat"]): r
            for r in rows
            if r["cell"]["arm_id"] == arm
        }
        for arm in [left, right]
    }
    groups = defaultdict(list)
    missing = 0
    for key in selected[left].keys() | selected[right].keys():
        a, b = selected[left].get(key), selected[right].get(key)
        x, y = (a or {}).get(metric), (b or {}).get(metric)
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            missing += 1
            continue
        assert a is not None and b is not None
        if a["cell"]["event_cluster_id"] != b["cell"]["event_cluster_id"]:
            raise ValueError("paired_event_identity_changed")
        groups[a["cell"]["event_cluster_id"]].append(y - x)
    values = [mean(group) for _, group in sorted(groups.items())]
    result = {
        "left": left,
        "right": right,
        "metric": metric,
        "statistical_unit": "equally_weighted_event_cluster_mean_of_paired_task_and_repeat_deltas",
        "paired_clusters": len(values),
        "missing_pairs": missing,
        "mean_delta": mean(values) if values else None,
        "interval_95": None,
        "limitation": None,
    }
    if missing:
        result["limitation"] = "missing_pairs_no_complete_denominator_comparison"
    elif len(values) < minimum:
        result["limitation"] = "too_few_independent_clusters_for_interval"
    else:
        rng = Random(seed)
        distribution = sorted(mean(rng.choices(values, k=len(values))) for _ in range(draws))
        result["interval_95"] = [
            distribution[int(draws * 0.025)],
            distribution[min(draws - 1, int(draws * 0.975))],
        ]
    return result


def failure_causes(row):
    causes = []
    report = row.get("report_summary") or {}
    error = report.get("error_code") or row.get("error_code") or ""
    termination = report.get("termination") or ""
    if row["state"] in {"not_started", "running", "unknown"}:
        causes.append("not_observed_or_unresolved")
    if "budget" in error + termination or "allowance" in error + termination:
        causes.append("resource_limit")
    if "context" in error + termination:
        causes.append("context_limit_or_binding")
    if "model" in error:
        causes.append("model_transport_or_contract")
    if "evidence" in error or "citation" in error:
        causes.append("evidence_integrity_or_constraints")
    if "authorization" in error or "access" in error:
        causes.append("authorization")
    if row["state"] == "failed" and not causes:
        causes.append("execution_failure")
    if row.get("formal_task_success") is False:
        causes.append("independently_reviewed_task_failure")
    return causes


def summarize(protocol, rows):
    expected = {c["cell_id"] for c in cells(protocol)}
    if {r["cell"]["cell_id"] for r in rows} != expected or len(rows) != len(expected):
        raise ValueError("analysis_requires_complete_frozen_denominator")
    by_arm = {}
    for arm in protocol.arms:
        subset = [r for r in rows if r["cell"]["arm_id"] == arm.arm_id]
        reviewed = [r for r in subset if r.get("formal_task_success") is not None]
        metrics = [r["formal_metrics"] for r in reviewed if r.get("formal_metrics")]
        totals = {
            name: sum(m.get(name, 0) for m in metrics)
            for name in [
                "supported_citation_pairs",
                "partial_citation_pairs",
                "unsupported_citation_pairs",
                "evaluated_citation_pairs",
                "claim_count",
                "fully_supported_claim_count",
                "unsupported_assertion_count",
                "partial_assertion_count",
            ]
        }
        semantic = None
        if len(reviewed) == len(subset):
            semantic = totals | {
                "citation_support_rate": totals["supported_citation_pairs"]
                / totals["evaluated_citation_pairs"]
                if totals["evaluated_citation_pairs"]
                else None,
                "unsupported_assertion_rate": totals["unsupported_assertion_count"]
                / totals["claim_count"]
                if totals["claim_count"]
                else None,
                "relation_judgments": dict(
                    Counter(
                        r["relation_type"] + ":" + r["verdict"]
                        for m in metrics
                        for r in m["relations"]
                    )
                ),
            }
        actual = [r.get("provider_cost") for r in subset]
        by_arm[arm.arm_id] = {
            "family": arm.family,
            "method": arm.method,
            "frozen_tasks_and_repeats": len(subset),
            "independent_event_clusters": len({r["cell"]["event_cluster_id"] for r in subset}),
            "states": dict(Counter(r["state"] for r in subset)),
            "mechanical_evidence_verified": sum(r.get("evidence_verified") is True for r in subset),
            "independent_reviews": len(reviewed),
            "formal_task_completion_rate": sum(r["formal_task_success"] for r in reviewed)
            / len(subset)
            if len(reviewed) == len(subset)
            else None,
            "formal_supported_claim_fraction": totals["fully_supported_claim_count"]
            / totals["claim_count"]
            if len(reviewed) == len(subset) and totals["claim_count"]
            else None,
            "formal_semantic_metrics": semantic,
            "provider_cost_total": sum(actual)
            if all(type(c) in {int, float} for c in actual)
            else None,
            "end_to_end_ms_observed_median": median(
                [r["end_to_end_ms"] for r in subset if r.get("end_to_end_ms") is not None]
            )
            if any(r.get("end_to_end_ms") is not None for r in subset)
            else None,
            "failure_causes": dict(Counter(c for r in subset for c in failure_causes(r))),
            "categories": {
                category: {"tasks": len(part), "states": dict(Counter(r["state"] for r in part))}
                for category in sorted({r["category"] for r in subset})
                if (part := [r for r in subset if r["category"] == category])
            },
        }
    comparisons = [
        paired_interval(
            rows,
            "fixed",
            method,
            metric,
            seed=protocol.random_seed,
            draws=protocol.bootstrap_draws,
            minimum=protocol.minimum_clusters_for_interval,
        )
        for method in ["single_agent", "parallel_tools", "multi_agent"]
        for metric in ["end_to_end_ms", "formal_task_success"]
    ]
    for family, field, baseline in [
        ("retrieval", "retrieval", "hybrid"),
        ("context", "context", "full_history"),
        ("memory_frozen", "memory", "none"),
        ("memory_sequential", "memory", "none"),
    ]:
        arms = [a for a in protocol.arms if a.family == family]
        base = next((a for a in arms if getattr(a, field) == baseline), None)
        if base:
            comparisons += [
                paired_interval(
                    rows,
                    base.arm_id,
                    a.arm_id,
                    metric,
                    seed=protocol.random_seed,
                    draws=protocol.bootstrap_draws,
                    minimum=protocol.minimum_clusters_for_interval,
                )
                for a in arms
                if a.arm_id != base.arm_id
                for metric in ["end_to_end_ms", "formal_task_success"]
            ]
    return {
        "arms": by_arm,
        "paired": comparisons,
        "unresolved_cells": sum(r["state"] in {"not_started", "running", "unknown"} for r in rows),
        "quality_cost_claim_allowed": all(
            r.get("formal_task_success") is not None
            and r.get("provider_cost") is not None
            and r.get("generation_mode") == "live_provider"
            for r in rows
        ),
        "limitations": [
            "Repeated runs do not add independent events.",
            "Mechanical completion and source replay are not semantic task success.",
            "Unknown costs and missing reviews remain null; no success-only exclusion.",
            "Intervals describe declared event clusters, whose real independence remains limited by the registry.",
        ],
    }

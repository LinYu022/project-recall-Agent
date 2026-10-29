"""Post-run, analysis-only supplement; never executes tasks or calls a provider.

This file is outside the sealed generation fingerprint. Its output is explicitly
post hoc and does not replace frozen observations, references, scoring or analysis.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from evaluation.analysis import paired_interval
from evaluation.freeze import load
from ingestion.records import content_hash
from investigation.agent_contracts import Decision
from investigation.contracts import EvidenceCard
from investigation.evidence import close_context, verify_proposal
from retrieval.contracts import SearchScope
from runtime.artifacts import ArtifactStore, native_path


def read(path):
    return json.loads(Path(path).read_bytes())


def provider_usage(entries, prices):
    names = [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ]
    result = {
        "requests_reserved": len(entries),
        "states": dict(Counter(e["state"] for e in entries)),
        "usage": {},
        "actual_account_charge": None,
    }
    for name in names:
        values = [(e.get("usage") or {}).get(name) for e in entries]
        observed = [v for v in values if type(v) is int and v >= 0]
        result["usage"][name] = {
            "known_subtotal": sum(observed),
            "observed_requests": len(observed),
            "missing_requests": len(values) - len(observed),
            "complete_total": sum(observed) if len(values) == len(observed) else None,
        }
    priced_names = list(prices["rates"]["peak"])
    complete = all(result["usage"][n]["complete_total"] is not None for n in priced_names)
    bounds = []
    if complete:
        for period in ["off_peak", "peak"]:
            bounds.append(
                str(
                    sum(
                        Decimal(result["usage"][n]["complete_total"])
                        * Decimal(prices["rates"][period][n])
                        / Decimal(1_000_000)
                        for n in priced_names
                    )
                )
            )
    result["advertised_rate_CNY_bounds_if_usage_complete"] = bounds if complete else None
    result["estimate_is_account_charge"] = False
    return result


def recheck_failed_proposal(observation):
    """Re-run only a pure local predicate on an exact saved proposal, not a task."""
    pointer = observation.get("report_reference")
    if not pointer:
        return {"status": "no_saved_report", "cause": None}
    store = ArtifactStore(Path(pointer["artifact_root"]))
    report = store.read_json(pointer["report_artifact"]["sha256"])
    assert content_hash(report) == observation["report_content_sha256"]
    last = (report.get("decisions") or [{}])[-1]
    if not last.get("output_artifact"):
        return {"status": "no_saved_response_to_recheck", "cause": None}
    response = store.read_json(last["output_artifact"]["sha256"])
    try:
        decision = Decision.model_validate(response["proposal"])
    except ValidationError as error:
        return {
            "status": "pure_decision_schema_reproduced_rejection",
            "cause": "saved_decision_schema_validation_failed",
            "validation_errors": error.errors(
                include_input=False, include_url=False, include_context=False
            ),
            "saved_response_artifact": last["output_artifact"],
        }
    if decision.action != "finish":
        return {"status": "no_final_proposal_to_recheck", "cause": None}
    try:
        cards = [EvidenceCard.model_validate(c) for c in report["cards"]]
        closed, _ = close_context(decision.proposal, cards, report["aliases"])
        verify_proposal(
            closed, cards, report["aliases"], SearchScope.model_validate(report["scope"])
        )
    except ValueError as error:
        return {
            "status": "pure_predicate_reproduced_rejection",
            "cause": str(error),
            "error_type": type(error).__name__,
            "saved_final_response_artifact": last["output_artifact"],
        }
    return {
        "status": "predicate_passed_cause_elsewhere_or_unknown",
        "cause": None,
        "saved_final_response_artifact": last["output_artifact"],
    }


def mechanism_observations(saved_observations):
    reports = []
    for observation in saved_observations:
        pointer = observation.get("report_reference")
        if pointer:
            report = ArtifactStore(Path(pointer["artifact_root"])).read_json(
                pointer["report_artifact"]["sha256"]
            )
            assert content_hash(report) == observation["report_content_sha256"]
            reports.append(report)
    policies = [r["delegation_policy"] for r in reports if r.get("delegation_policy")]
    branches = [b for r in reports for b in (r.get("branch_results") or [])]
    return {
        "saved_report_count": len(reports),
        "tool_batches_field_present": sum("tool_batches" in r for r in reports),
        "reports_with_recorded_tool_batch": sum(bool(r.get("tool_batches")) for r in reports),
        "recorded_tool_batches": sum(len(r.get("tool_batches") or []) for r in reports),
        "reports_with_delegation_policy": len(policies),
        "delegate_true_reports": sum(p.get("delegate") is True for p in policies),
        "delegate_false_reports": sum(p.get("delegate") is False for p in policies),
        "branch_result_records": sum(len(r.get("branch_results") or []) for r in reports),
        "branch_status_counts": dict(Counter(b.get("status", "missing") for b in branches)),
        "branch_nonempty_error_codes": dict(
            Counter(b["error_code"] for b in branches if b.get("error_code"))
        ),
        "reports_with_any_branch_error": sum(
            any(b.get("error_code") for b in (r.get("branch_results") or [])) for r in reports
        ),
        "interpretation": "Recorded activation only; missing reports or fields do not prove nonactivation, and enabling a method is not proof its mechanism was used.",
    }


def paired_resources(protocol, index, ledger, prices, observations):
    rows = []
    metrics = [
        "end_to_end_ms",
        "requests_reserved",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ]
    metrics += ["advertised_CNY_lower", "advertised_CNY_upper"]
    for saved in index["rows"].values():
        entries = [e for e in ledger["entries"] if e["cell"] == saved["cell_id"]]
        resource = provider_usage(entries, prices)
        unresolved = saved["state"] in {"not_started", "running", "unknown"}
        values = {
            "requests_reserved": None if unresolved else len(entries),
            "end_to_end_ms": None
            if unresolved
            else observations.get(saved["cell_id"], {}).get("end_to_end_ms"),
        }
        for name in ["prompt_tokens", "completion_tokens", "total_tokens"]:
            values[name] = None if unresolved else resource["usage"][name]["complete_total"]
        bounds = resource["advertised_rate_CNY_bounds_if_usage_complete"]
        for i, name in enumerate(["advertised_CNY_lower", "advertised_CNY_upper"]):
            values[name] = float(bounds[i]) if bounds is not None and not unresolved else None
        rows.append(
            {
                "cell": {
                    k: saved[k]
                    for k in ["cell_id", "arm_id", "case_id", "repeat", "event_cluster_id"]
                },
                "state": saved["state"],
                **values,
            }
        )
    pairs = [("fixed", other) for other in ["single_agent", "parallel_tools", "multi_agent"]]
    for family, field, baseline in [
        ("retrieval", "retrieval", "hybrid"),
        ("context", "context", "full_history"),
        ("memory_frozen", "memory", "none"),
        ("memory_sequential", "memory", "none"),
    ]:
        arms = [a for a in protocol.arms if a.family == family]
        base = next(a for a in arms if getattr(a, field) == baseline)
        pairs.extend((base.arm_id, a.arm_id) for a in arms if a.arm_id != base.arm_id)
    return {
        "classification": "post_hoc_resource_analysis_not_confirmatory_quality_ranking",
        "delta_direction": "right_minus_left",
        "all_frozen_cells_retained": True,
        "unresolved_cells_are_null_not_zero": True,
        "advertised_price_bounds_are_not_actual_account_charges": True,
        "price_delta_interpretation": "Lower-vs-lower and upper-vs-upper common-rate scenarios, not bounds on the actual between-method billing difference; actual request tariff times are unknown.",
        "rows": rows,
        "comparisons": [
            paired_interval(
                rows,
                left,
                right,
                metric,
                seed=protocol.random_seed,
                draws=protocol.bootstrap_draws,
                minimum=protocol.minimum_clusters_for_interval,
            )
            for left, right in pairs
            for metric in metrics
        ],
    }


def preparation_observations(observations):
    result = []
    for cell_id, observation in observations.items():
        if observation["cell"]["family"] not in {"context", "memory_frozen", "memory_sequential"}:
            continue
        prefix = observation.get("prefix_preparation")
        setup, end = observation.get("setup_ms"), observation.get("end_to_end_ms")
        numeric = type(setup) in {int, float} and type(end) in {int, float}
        if numeric:
            assert end >= setup >= 0
        report = {}
        pointer = observation.get("report_reference")
        if pointer:
            report = ArtifactStore(Path(pointer["artifact_root"])).read_json(
                pointer["report_artifact"]["sha256"]
            )
            assert content_hash(report) == observation["report_content_sha256"]
        row = {
            "cell_id": cell_id,
            "state": observation["state"],
            "setup_ms": setup,
            "end_to_end_ms": end,
            "post_setup_observed_ms": end - setup if numeric else None,
            "prefix_reused": observation.get("prefix_reused"),
            "prefix_preparation_recorded": prefix is not None,
            "prefix_scripted_flows": len(prefix["flows"]) if prefix is not None else None,
            "prefix_provider_calls": prefix.get("provider_calls") if prefix is not None else None,
            "prefix_target_question_or_labels_used": prefix.get("target_question_or_labels_used")
            if prefix is not None
            else None,
            "semantic_usage_field_present": "memory_usage" in report,
            "semantic_reference_records": len(report["memory_usage"]["references"])
            if "memory_usage" in report
            else None,
            "history_usage_field_present": "history_usage" in report,
            "history_reference_records": len(report["history_usage"]["references"])
            if "history_usage" in report
            else None,
            "history_preparation": report.get("history_preparation"),
            "skill_usage_field_present": "skill_usage" in report,
            "skill_application_attempted": (report.get("skill_usage") or {}).get(
                "application_attempted"
            ),
            "skill_application_records": len(report["skill_usage"].get("applications", []))
            if "skill_usage" in report
            else None,
            "context_builds": report.get("context_builds"),
            "interpretation": "Preparation and adoption observations only; no semantic usefulness inferred. Missing measurements stay null; post-setup difference is not pure model time.",
        }
        result.append(row)
    return result


def prefix_validation_failures(identity, observations):
    from memory.procedure_engine import event_groups

    result = []
    for cell_id, observation in observations.items():
        error = observation.get("error_code") or ""
        prefix = "ValueError:frozen_prefix_procedure_validation_failed:"
        if not error.startswith(prefix):
            continue
        key = content_hash(observation["cell"])[:12]
        control_root = Path(".local/e16") / identity["experiment_id"][:12] / "l" / key
        prefix_sha = error[len(prefix) :]
        bundle = ArtifactStore(control_root / "prefix").read_json(prefix_sha)
        validation = bundle["validation"]
        pointer = validation["proof"]
        proof = ArtifactStore(Path(pointer["artifact_root"])).read_json(
            pointer["artifact"]["sha256"]
        )
        lookups = []
        for attempt in proof.get("attempts", []):
            store = ArtifactStore(Path(pointer["artifact_root"]) / attempt["task_id"])
            for path in native_path(store.directory).glob("*.json"):
                value = store.read_json(path.stem)
                if value.get("tool") != "lookup":
                    continue
                output = value["output"]
                groups = event_groups([{"kind": "structured_lookup", "payload": output}])
                lookups.append(
                    {
                        "artifact_sha256": path.stem,
                        "counts": output.get("counts"),
                        "matched_event_groups": groups,
                        "pending_reasons": sorted(
                            {
                                reason
                                for row in output.get("rows", [])
                                if row["channel"] == "pending"
                                for reason in row["reasons"]
                            }
                        ),
                        "no_matched_event_identity_established": not groups,
                    }
                )
        result.append(
            {
                "cell_id": cell_id,
                "prefix_bundle_sha256": prefix_sha,
                "prefix_scripted_flows": len(bundle["flows"]),
                "validation_proof": pointer,
                "original_failure_code": proof.get("failure_code"),
                "validation_state": proof["validation_state"],
                "new_model_calls": proof["new_model_calls"],
                "transfer_lookups": lookups,
                "interpretation": "The frozen overlap error also covers an empty event set. Empty matched_event_groups shows unknown target identity, not proven overlap; preserve original code and lookup proof.",
            }
        )
    return result


def execution_closure(protocol, index, observations, ledger, helper):
    finished = bool(helper and helper.get("server_stopped") and "exit_code" in helper)
    unresolved = [
        r for r in index["rows"].values() if r["state"] in {"not_started", "running", "unknown"}
    ]
    dependency_blocks = []
    if finished:
        for row in unresolved:
            if row["state"] != "not_started" or row["family"] != "memory_sequential":
                continue
            arm = next(a for a in protocol.arms if a.arm_id == row["arm_id"])
            prior = [
                r
                for r in index["rows"].values()
                if r["arm_id"] == row["arm_id"]
                and r["repeat"] == row["repeat"]
                and arm.cases.index(r["case_id"]) < arm.cases.index(row["case_id"])
            ]
            failed = [
                r
                for r in prior
                if r["state"] == "failed"
                and (observations.get(r["cell_id"], {}).get("error_code") or "").startswith(
                    "ValueError:frozen_prefix_procedure_validation_failed:"
                )
            ]
            if not failed:
                continue
            entries = [e for e in ledger["entries"] if e["cell"] == row["cell_id"]]
            assert not entries and row["record"] is None
            dependency_blocks.append(
                {
                    "cell_id": row["cell_id"],
                    "original_state": "not_started",
                    "original_observation": None,
                    "derived_classification": "not_executed_dependency_blocked",
                    "reason": "Frozen sequential runner breaks before claim when prerequisite prefix validation failed and sequence context was never created.",
                    "failed_predecessors": [
                        {"cell_id": r["cell_id"], "observation_artifact": r["record"]}
                        for r in failed
                    ],
                    "actual_provider_requests": 0,
                    "execution_elapsed_ms": None,
                    "original_index_mutated": False,
                }
            )
    blocked_ids = {r["cell_id"] for r in dependency_blocks}
    remaining = [r["cell_id"] for r in unresolved if r["cell_id"] not in blocked_ids]
    reservations = sum(e["state"] == "reserved" for e in ledger["entries"])
    return {
        "executor_finished": finished,
        "executor_exit_code": helper.get("exit_code") if helper else None,
        "original_unresolved_cell_count": len(unresolved),
        "derived_dependency_blocks": dependency_blocks,
        "other_unresolved_cells": remaining,
        "provider_reserved_unknown_count": reservations,
        "execution_accounting_closed": finished and not remaining and not reservations,
        "all_frozen_cells_have_terminal_observations": not unresolved,
        "interpretation": "Analysis-only dependency accounting; never fabricates an execution observation, elapsed time, or task success for an unstarted cell. Does not establish semantic acceptance.",
    }


def database_snapshot(identity, index):
    """Read selected dispatch fields without printing credentials or changing state."""
    import sqlalchemy as sa
    from sqlalchemy.engine import URL

    from control.queue_schema import calls, jobs

    settings = read(
        Path(".local/e16-db") / identity["experiment_id"][:12] / "l/private-settings.json"
    )
    url = URL.create(
        "postgresql+psycopg",
        username="recall_eval",
        password=settings["password"],
        host="127.0.0.1",
        port=settings["port"],
        database="postgres",
    )
    engine = sa.create_engine(
        url,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-csearch_path=evaluation_" + identity["experiment_id"][:24] + "_live",
        },
    )
    rows = []
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            for saved in index["rows"].values():
                if saved["state"] != "failed":
                    continue
                cell = {
                    k: saved[k]
                    for k in [
                        "arm_id",
                        "case_id",
                        "cell_id",
                        "event_cluster_id",
                        "family",
                        "method",
                        "repeat",
                    ]
                }
                task_id = "EVAL-" + content_hash(cell)[:24].upper()
                dispatch = (
                    connection.execute(
                        sa.select(jobs.c.job_id, jobs.c.state, jobs.c.last_error).where(
                            jobs.c.task_id == task_id
                        )
                    )
                    .mappings()
                    .all()
                )
                journal = (
                    connection.execute(
                        sa.select(
                            calls.c.kind,
                            calls.c.ordinal,
                            calls.c.state,
                            calls.c.error_code,
                            calls.c.pending_reconciliation,
                            calls.c.input_artifact,
                            calls.c.output_artifact,
                        ).where(calls.c.job_id.in_([j["job_id"] for j in dispatch]))
                    )
                    .mappings()
                    .all()
                )
                rows.append(
                    {
                        "cell_id": saved["cell_id"],
                        "jobs": [dict(j) for j in dispatch],
                        "journal": [dict(c) for c in journal],
                    }
                )
    finally:
        engine.dispose()
    return {
        "at": datetime.now(UTC).isoformat(),
        "transaction_read_only": True,
        "not_state_at_original_observation_time": True,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-running-db", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/verification/2026-10-28-P1-16-analysis-supplement.json"),
    )
    args = parser.parse_args()
    identity = read(".local/P1-16/active-experiment.json")
    body, protocol = load(identity)
    root = Path(identity["artifact_root"]) / "live"
    index = read(root / "runs.json")
    assert set(index["rows"]) == {c["cell_id"] for c in body["cells"]}
    store = ArtifactStore(root)
    observations = {}
    for cell_id, row in index["rows"].items():
        if row.get("record"):
            value = store.read_json(row["record"]["sha256"])
            assert value["cell"]["cell_id"] == cell_id and value["state"] == row["state"]
            observations[cell_id] = value
    ledger = read(".local/P1-16/provider-request-allowance.json")
    assert ledger["spec"]["experiment_id"] == identity["experiment_id"]
    assert len(ledger["entries"]) <= body["maximum_provider_requests"]
    assert [e["ticket"] for e in ledger["entries"]] == list(range(len(ledger["entries"])))
    counts = Counter(e["cell"] for e in ledger["entries"])
    assert all(n <= ledger["spec"]["cells"][cell] for cell, n in counts.items())
    for cell in counts:
        entries = [e for e in ledger["entries"] if e["cell"] == cell]
        spent = 0
        for entry in entries:
            completion = (entry.get("usage") or {}).get("completion_tokens")
            spent += (
                completion if type(completion) is int and completion >= 0 else entry["max_tokens"]
            )
        assert spent <= ledger["spec"]["output_limit_per_cell"]
    provider = ArtifactStore(root / "provider")
    provider_errors = []
    for entry in ledger["entries"]:
        if entry.get("artifact"):
            value = provider.read_json(entry["artifact"]["sha256"])
            sent = provider.read_json(value["input_artifact"]["sha256"])
            assert content_hash(sent) == entry["fingerprint"]
            assert sent["max_tokens"] == entry["max_tokens"]
            assert sent["identity"] == ledger["spec"]["model_identity"]
            assert entry.get("usage") == (
                value.get("result") or value.get("diagnostics") or {}
            ).get("usage")
            if entry["state"] == "failed":
                provider_errors.append(
                    {
                        "ticket": entry["ticket"],
                        "cell_id": entry["cell"],
                        "response_artifact": entry["artifact"],
                        "error_code": value.get("error_code"),
                        "finish_reason": (value.get("diagnostics") or {}).get("finish_reason"),
                        "max_tokens": entry["max_tokens"],
                        "usage": entry.get("usage"),
                        "counts_against_request_allowance": True,
                    }
                )
        usage = entry.get("usage") or {}
        if all(
            type(usage.get(k)) is int
            for k in ["prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"]
        ):
            assert usage["prompt_tokens"] == (
                usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
            )
        if all(
            type(usage.get(k)) is int
            for k in ["prompt_tokens", "completion_tokens", "total_tokens"]
        ):
            assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    price_path = Path("docs/verification/2026-10-28-P1-16-advertised-pricing.json")
    prices = read(price_path)
    receipt = prices["source"]
    assert (
        hashlib.sha256(Path(receipt["local_archive"]).read_bytes()).hexdigest() == receipt["sha256"]
    )
    arms = {}
    failures = []
    for arm in protocol.arms:
        rows = [r for r in index["rows"].values() if r["arm_id"] == arm.arm_id]
        arms[arm.arm_id] = {
            "family": arm.family,
            "frozen_denominator": len(rows),
            "states": dict(Counter(r["state"] for r in rows)),
            "mechanism_observations": mechanism_observations(
                [observations[r["cell_id"]] for r in rows if r["cell_id"] in observations]
            ),
            "provider": provider_usage(
                [e for e in ledger["entries"] if index["rows"][e["cell"]]["arm_id"] == arm.arm_id],
                prices,
            ),
        }
    for cell_id, value in observations.items():
        if value["state"] != "failed":
            continue
        requests = [e for e in ledger["entries"] if e["cell"] == cell_id]
        failures.append(
            {
                "cell_id": cell_id,
                "original_observation_artifact": index["rows"][cell_id]["record"],
                "original_error_code": value.get("error_code"),
                "original_report_error_code": (value.get("report_summary") or {}).get("error_code"),
                "actual_provider_requests": len(requests),
                "provider_cell_cap": ledger["spec"]["cells"][cell_id],
                "pure_local_proposal_recheck": recheck_failed_proposal(value),
            }
        )
    human = read("docs/verification/2026-10-28-P1-16-human-review.json")
    assert human["experiment_id"] == identity["experiment_id"]
    matches = [o for o in observations.values() if o.get("blind_id") == human["blind_id"]]
    assert len(matches) == 1 and content_hash(matches[0]) == human["observation_sha256"]
    retrieval = read("docs/verification/2026-10-28-P1-16-retrieval.json")
    assert retrieval["protocol_sha256"] == content_hash(protocol.model_dump(mode="json"))
    state = (
        read(Path(".local/e16-db") / identity["experiment_id"][:12] / "l/status.json")
        if (Path(".local/e16-db") / identity["experiment_id"][:12] / "l/status.json").exists()
        else None
    )
    result = {
        "schema_version": "P1-16-posthoc-analysis-supplement/0.1",
        "classification": "implementation_agent_analysis_not_independent_semantic_scoring",
        "analysis_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "exported_at": datetime.now(UTC).isoformat(),
        "experiment_id": identity["experiment_id"],
        "snapshot_is_final": not any(
            r["state"] in {"not_started", "running", "unknown"} for r in index["rows"].values()
        )
        and not any(e["state"] == "reserved" for e in ledger["entries"]),
        "frozen_denominator": len(index["rows"]),
        "states": dict(Counter(r["state"] for r in index["rows"].values())),
        "run_index_content_sha256": content_hash(index),
        "ledger_content_sha256": content_hash(ledger),
        "provider": provider_usage(ledger["entries"], prices),
        "provider_unknown_reservations": sum(e["state"] == "reserved" for e in ledger["entries"]),
        "provider_response_errors": provider_errors,
        "arms": arms,
        "paired_resource_analysis": paired_resources(protocol, index, ledger, prices, observations),
        "preparation_and_adoption_observations": preparation_observations(observations),
        "prefix_validation_failures": prefix_validation_failures(identity, observations),
        "failures": failures,
        "complete_qualitative_human_reviews": human["qualitative_human_reviews"],
        "human_reviewed_claims": human["reviewed_claims"],
        "human_annotation_active_elapsed_ms": None,
        "human_annotation_time_status": "not_measured_do_not_impute_zero_or_infer_from_message_timestamps",
        "uniform_semantic_metrics": None,
        "human_supplement_content_sha256": content_hash(human),
        "retrieval_supplement_content_sha256": content_hash(retrieval),
        "price_supplement_content_sha256": content_hash(prices),
        "local_helper_status": state,
        "execution_closure": execution_closure(protocol, index, observations, ledger, state),
        "limitations": [
            "Post hoc pure predicate rechecks explain specific saved failures; they do not change scores or establish semantic correctness.",
            "A failed proposal may have more errors after the first rejection; do not infer exhaustive diagnosis.",
            "Snapshot totals may grow while the approved batch runs; unstarted cells remain in the denominator.",
            "Advertised-rate bounds assume the public CNY rates apply; account currency, discounts and actual charges are not verified.",
        ],
    }
    if args.read_running_db:
        result["dispatch_readonly_snapshot"] = database_snapshot(identity, index)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "snapshot_is_final": result["snapshot_is_final"],
                "states": result["states"],
                "provider_requests": len(ledger["entries"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reconcile P1-16 delivery separately from its unresolved model-quality acceptance."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from evaluation.freeze import load
from ingestion.records import content_hash
from reviewing.corpus import read, verified, write_new
from runtime.artifacts import ArtifactStore


def delivery_summary(raw, supplement, development, components, targets):
    states = dict(Counter(r["state"] for r in raw["rows"]))
    if len(raw["rows"]) != 270 or states != {
        "completed": 85,
        "guarded": 120,
        "failed": 64,
        "not_started": 1,
    }:
        raise ValueError("original_full_denominator_or_states_changed")
    baseline = development["quality_baseline"]
    if development["human_reviewed_cells"] != 8 or baseline["reviewed_tasks"] != 8:
        raise ValueError("development_review_not_complete")
    methods = {}
    for method in ("fixed", "single_agent", "parallel_tools", "multi_agent"):
        arm = raw["summary"]["arms"][method]
        methods[method] = {
            "denominator": arm["frozen_tasks_and_repeats"],
            "states": arm["states"],
            "requests": supplement["arms"][method]["provider"]["requests_reserved"],
            "mechanism": supplement["arms"][method]["mechanism_observations"],
            "formal_population_task_success": None,
        }
        if sum(methods[method]["states"].values()) != 60:
            raise ValueError("main_method_denominator_changed")
    return {
        "module": "P1-16",
        "delivery_status": "engineering_experiment_and_report_delivery_closed_with_quality_reservations",
        "module_delivery_complete": True,
        "all_original_quality_requirements_passed": False,
        "formal_model_quality_acceptance": "not_established",
        "original_experiment_id": raw["experiment"]["experiment_id"]
        if isinstance(raw["experiment"], dict)
        else raw["experiment"],
        "original_frozen_cells": 270,
        "original_states": states,
        "original_provider_requests": 209,
        "development_provider_requests": development["actual_new_provider_requests"],
        "P1_16_requests_across_two_batches": 209 + development["actual_new_provider_requests"],
        "new_provider_requests_for_delivery": 0,
        "main_methods": methods,
        "other_arms": {
            k: {"denominator": v["frozen_tasks_and_repeats"], "states": v["states"]}
            for k, v in raw["summary"]["arms"].items()
            if k not in methods
        },
        "engineering_challenges": raw["challenge_results"],
        "independently_reviewed_development": baseline,
        "human_test_reviewed_cells": 1,
        "direct_aspect_sample_components": {
            k: components[k]
            for k in (
                "reviewed_answer_tasks",
                "citation_pairs_reviewed",
                "direct_aspect_support_judgments",
                "direct_aspect_supported",
                "direct_aspect_support_fraction",
            )
        },
        "population_citation_support_rate": None,
        "atomic_unsupported_rate": None,
        "strict_population_task_success": None,
        "actual_account_charge": None,
        "prospective_targets_status": targets["status"],
        "new_preregistered_unseen_trial_executed": False,
        "new_trial_required_to_support_preregistered_quality_acceptance": True,
        "default_strategy": "fixed_retained_no_unproven_mechanism_benefit_claim",
        "unmet_quality_requirements": [
            "Original real test ran before an independently reviewed numerical development baseline existed.",
            "The 270-cell population has not received complete independent semantic component scoring.",
            "Atomic-assertion and whole-compound-claim citation metrics remain unmeasured.",
            "The new numerical targets are a draft; no replacement unseen experiment has been performed.",
        ],
        "features_or_original_goals_removed": False,
        "P1_17_started": False,
    }


def render_overview(summary, directory):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), constrained_layout=True)
    methods = list(summary["main_methods"])
    bottom = [0] * len(methods)
    for state, color in [("completed", "#41876b"), ("guarded", "#d3a34b"), ("failed", "#ba6262")]:
        values = [summary["main_methods"][m]["states"].get(state, 0) for m in methods]
        bars = axes[0].bar(methods, values, bottom=bottom, label=state, color=color)
        axes[0].bar_label(
            bars, labels=[str(v) if v else "" for v in values], label_type="center", color="white"
        )
        bottom = [a + b for a, b in zip(bottom, values, strict=True)]
    axes[0].set(
        title="Main trial: execution states, not semantic success",
        ylabel="Frozen cells per method",
        ylim=(0, 65),
    )
    axes[0].tick_params(axis="x", labelrotation=15)
    axes[0].legend(loc="lower center", bbox_to_anchor=(0.5, -0.28), ncols=3, frameon=False)
    axes[1].axis("off")
    text = (
        "FULL MATRIX: 270 cells\n85 completed | 120 guarded | 64 failed | 1 unstarted\n\n"
        "REAL PROVIDER REQUESTS\n209 original + 3 development = 212\n\n"
        "HUMAN-REVIEWED DEVELOPMENT (one person, two events)\n3/3 answer cores complete; 5/5 stage handling appropriate\n7 requirements satisfied; 1 qualified with minor issue\n\n"
        "UNMEASURED / UNVERIFIED\nPopulation semantic quality, atomic support, account charge\n\n"
        "DELIVERY CLOSED; PREREGISTERED QUALITY NOT ESTABLISHED"
    )
    axes[1].text(0, 1, text, va="top", fontsize=9.1, linespacing=1.6)
    fig.suptitle("P1-16 | traceable observations and explicit quality limitations", fontsize=13)
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    with TemporaryDirectory() as temp:
        for suffix in ("png", "pdf"):
            path = Path(temp) / ("overview." + suffix)
            metadata = (
                {"Creator": "P1-16 delivery", "CreationDate": None, "ModDate": None}
                if suffix == "pdf"
                else {"Software": "P1-16 delivery"}
            )
            fig.savefig(path, dpi=160, metadata=metadata)
            data = path.read_bytes()
            target = directory / path.name
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError("figure_changed_preserve_old_version")
            else:
                with target.open("xb") as stream:
                    stream.write(data)
            results.append(
                {
                    "path": target.as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
    plt.close(fig)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--postgres", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = {
        "original": "docs/verification/2026-10-28-P1-16-evaluation.json",
        "supplement": "docs/verification/2026-10-28-P1-16-analysis-supplement.json",
        "retrieval": "docs/verification/2026-10-28-P1-16-retrieval.json",
        "development": "docs/verification/2026-10-29-P1-16-development-review-complete.json",
        "components": "docs/verification/2026-10-29-P1-16-component-review.json",
        "targets": "evals/development-baseline-v0.1/quality-targets-draft.json",
    }
    records = {k: read(p) for k, p in sources.items()}
    identity = read(".local/P1-16/active-experiment.json")
    load(identity)
    _, _, _, rows, _ = verified(
        read(
            ".local/P1-16/role-aware-reviews/137288e5a173ef36db82beb91b0bdf39a0237f5d20e1e5a64e29bf62b94ea752.json"
        )
    )
    assert len(rows) == 270 and sum(r["observation"] is not None for r in rows) == 269
    assert (
        content_hash(read(".local/P1-16/provider-request-allowance.json"))
        == records["original"]["raw_records"]["ledger"]
    )
    assert (
        content_hash(read(".local/P1-16/development-baseline-provider-allowance.json"))
        == records["development"]["provider_ledger_content_sha256"]
    )
    assert (
        content_hash(read(".local/P1-09/request-allowance.json"))
        == records["original"]["raw_records"]["legacy_ledger"]
    )
    for source in records["development"]["actual_human_review_records"]:
        assert content_hash(read(source["path"])) == source["content_sha256"]
    offline, postgres = read(args.offline), read(args.postgres)
    assert all(c["exit_code"] == 0 for c in offline["checks"])
    assert not any(offline["tests"][k] for k in ("failures", "errors", "skipped"))
    assert not any(postgres["tests"][k] for k in ("failures", "errors", "skipped"))
    assert (
        offline["http_server_stopped"]
        and offline["query_http_server_stopped"]
        and postgres["server_stopped"]
    )
    assert all(c["status"] == "passed" for c in records["original"]["challenge_results"])
    assert (
        records["retrieval"]["row_count"] == records["retrieval"]["trace_artifacts_verified"] == 80
    )
    retrieval_source = read(records["retrieval"]["source_record"])
    assert content_hash(retrieval_source) == records["retrieval"]["source_record_content_sha256"]
    trace_store = ArtifactStore(Path(retrieval_source["trace_root"]))
    for row in records["retrieval"]["rows"]:
        trace_store.read_json(row["trace"]["sha256"])
    passed_names = set()
    for path in [Path(offline["logs"]) / "tests.xml", Path(postgres["artifacts"]) / "tests.xml"]:
        for testcase in ElementTree.parse(path).getroot().iter("testcase"):
            if not any(testcase.find(k) is not None for k in ("failure", "error", "skipped")):
                passed_names.add(testcase.attrib["name"])
    current_challenges = []
    for challenge in records["original"]["challenge_results"]:
        assert challenge["test"] in passed_names
        current_challenges.append(
            {"id": challenge["id"], "test": challenge["test"], "status": "passed_current_recheck"}
        )
    value = delivery_summary(
        records["original"],
        records["supplement"],
        records["development"],
        records["components"],
        records["targets"],
    )
    value |= {
        "schema_version": "P1-16-delivery-assessment/0.1",
        "source_fingerprints": {
            k: {"path": sources[k], "content_sha256": content_hash(v)} for k, v in records.items()
        },
        "generator_freeze_reverified": True,
        "retrieval_trace_artifacts_reverified": 80,
        "challenges_current_recheck": current_challenges,
        "final_offline_verification": offline,
        "final_postgres_verification": postgres,
        "retrieval_cluster_means": records["retrieval"]["cluster_means"],
        "paired_statistics": records["original"]["summary"]["paired"],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    key = content_hash({"sources": value["source_fingerprints"], "script": value["script_sha256"]})
    value["figures"] = render_overview(
        value, Path("docs/experiments/P1-16-delivery-figures") / key[:12]
    )
    write_new(args.output, value)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "delivery": value["delivery_status"],
                "quality_acceptance": value["formal_model_quality_acceptance"],
            }
        )
    )


if __name__ == "__main__":
    main()

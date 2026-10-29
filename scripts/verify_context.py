"""Zero-network context ablation: synthetic retention and saved official source replay."""

import argparse
import json
from pathlib import Path

from ingestion.records import content_hash
from investigation.agent import CONTEXT_SYSTEM
from investigation.contracts import EvidenceCard
from investigation.flow import prompt_cards
from investigation.inspection import inspect_report
from investigation.prompt_context import (
    ContextArchive,
    ContextBinding,
    build_context,
    json_text,
    restore_summary,
)
from releases.bundle import BundleStore
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.test_prompt_context import fixture_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--official",
        action="store_true",
        help="Requires the locally retained P1-09 release and reports",
    )
    parser.add_argument("--output", type=Path, default=Path(".local/P1-10/context-comparison.json"))
    args = parser.parse_args()
    root = Path(".local/P1-10")
    binding, cards, payload, _ = fixture_data()
    modes = []
    for mode in ("full_history", "recent_window", "structured"):
        archive = ContextArchive(ArtifactStore(root / "synthetic" / mode))
        value, record = build_context(
            CONTEXT_SYSTEM, payload, cards, binding, {"E002"}, archive, mode=mode, cap=24000
        )
        restore_summary(
            archive.store,
            record["summary_artifact"],
            binding,
            expected_snapshot=record["snapshot_artifact"]["sha256"],
        )
        record |= {
            "counter_text_retained": "does NOT establish" in json_text(value),
            "vehicle_year_retained": "2010" in json_text(value),
            "fits_24000_characters": record["after_characters"] <= 24000,
        }
        modes.append(record)
    assert modes[-1]["counter_text_retained"] and modes[-1]["fits_24000_characters"]
    assert not modes[1]["counter_text_retained"]
    assert not modes[0]["fits_24000_characters"]
    result = {
        "schema_version": "context-verification/0.1",
        "classification": "synthetic_retention_checks_not_model_quality",
        "synthetic_modes": modes,
        "official": None,
        "new_api_requests": 0,
        "new_source_requests": 0,
        "summary_model_calls": 0,
        "summary_provider_usage": None,
        "estimated_cost": None,
        "formal_semantic_metrics": None,
        "independent_human_reviews": 0,
    }
    if args.official:
        prior = json.loads(Path("docs/experiments/P1-09-results.json").read_bytes())
        run = next(
            r
            for r in prior["rounds"][-1]["runs"]
            if r["case_id"] == "DEV-PRIUS-JOINT" and r["method"] == "single_agent"
        )
        digest = run["report_artifact"]["sha256"]
        matches = list(Path(".local/P1-09/runs/private-runs").rglob(digest + ".json"))
        assert len(matches) == 1
        store = ArtifactStore(matches[0].parent.parent)
        report = store.read_json(digest)
        assert content_hash(report) == run["report_content_sha256"]
        bundles = BundleStore(Path(".local/releases"))
        manifest = bundles.verify(report["manifest_sha256"], report["release_id"])
        base = bundles.directory(manifest)
        cards = [EvidenceCard.model_validate(c) for c in report["cards"]]
        aliases, visible = prompt_cards(cards)
        originals = 0
        for alias in aliases:
            inspect_report(report, store, base, section="evidence", alias=alias, limit=4000)
            originals += 1
        payload = store.read_json(report["decisions"][-1]["input_artifact"]["sha256"])["payload"]
        payload |= {
            "evidence": visible,
            "observations": report["observations"],
            "previous_decisions": report["planner_revisions"],
        }
        binding = ContextBinding(
            task_id=report["request"]["task_id"],
            release_id=report["release_id"],
            task_spec_sha256=report["task_spec_sha256"],
            plan_sha256=content_hash(payload["initial_plan"]),
        )
        payload["authorization_limits"] = {
            "allowed_source_ids": report["allowed_source_ids"],
            "scope": report["scope"],
            "time": report["time"],
        }
        official = []
        for mode in ("full_history", "recent_window", "structured"):
            archive = ContextArchive(ArtifactStore(root / "official" / mode))
            value, record = build_context(
                CONTEXT_SYSTEM,
                payload,
                cards,
                binding,
                set(report["recorded_counterevidence_aliases"]),
                archive,
                mode=mode,
                cap=24000,
            )
            summary = restore_summary(
                archive.store,
                record["summary_artifact"],
                binding,
                expected_snapshot=record["snapshot_artifact"]["sha256"],
            )
            assert (
                len(summary.entries) == 8
            )  # one lookup plus seven exact source/version text groups
            value_text = json_text(value)
            record |= {
                "fits_24000_characters": record["after_characters"] <= 24000,
                "source_cards": len(cards),
                "deduplicated_groups": len(summary.entries),
                "visible_source_versions": sorted(
                    {v for e in value["evidence"] for v in e.get("source_version_ids", [])}
                ),
            }
            if mode == "structured":
                assert "monitor" in value_text.lower() and "2010" in value_text
                assert record["after_characters"] <= 24000
                assert value["context_summary"]["omitted_evidence_groups"] == 0
                try:
                    restore_summary(
                        archive.store,
                        record["summary_artifact"],
                        binding.model_copy(update={"revision_id": "STALE"}),
                        expected_snapshot=record["snapshot_artifact"]["sha256"],
                    )
                except ArtifactIntegrityError:
                    record["stale_revision_rejected"] = True
                else:
                    raise AssertionError("stale revision accepted")
            official.append(record)
        result["official"] = {
            "classification": "saved_real_NHTSA_sources_reconstructed_unsent_next_context_no_generation",
            "source_report_content_sha256": content_hash(report),
            "release_id": report["release_id"],
            "original_evidence_replays": originals,
            "modes": official,
            "limitations": [
                "This reconstructs the next input from retained run state; it was not sent to a provider.",
                "Context size and exact source retention do not demonstrate improved model answers.",
                "The recent window intentionally drops early evidence and is an ablation, not the default.",
            ],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "new_api_requests": 0,
                "official_source_replays": result["official"]["original_evidence_replays"]
                if args.official
                else 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

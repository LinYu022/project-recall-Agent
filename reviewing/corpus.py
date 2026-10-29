"""Read frozen cells without executing, reopening SQL, or changing the old review index."""

import hashlib
import json
from pathlib import Path

from evaluation.freeze import load
from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from reviewing.policy import POLICY, implementation
from reviewing.scoring import assess, context_links
from runtime.artifacts import ArtifactStore, native_path


def read(path):
    return json.loads(native_path(Path(path)).read_bytes())


def write_new(path, value):
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    native = native_path(path)
    native.parent.mkdir(parents=True, exist_ok=True)
    if native.exists():
        if native.read_text(encoding="utf-8") != data:
            raise ValueError("refuse_overwrite_analysis_or_review_artifact")
    else:
        with native.open("x", encoding="utf-8") as stream:
            stream.write(data)


def load_cells(identity_path, labels=Path("evals/comprehensive-v0.1")):
    identity = read(identity_path)
    body, protocol = load(identity)
    root = Path(identity["artifact_root"]) / "live"
    index = read(root / "runs.json")
    if index["experiment_id"] != identity["experiment_id"] or index["mode"] != "live":
        raise ValueError("run_index_identity_changed")
    expected = {c["cell_id"]: c for c in body["cells"]}
    if set(index["rows"]) != set(expected):
        raise ValueError("incomplete_frozen_cell_denominator")
    by_case = {c.case_id: c for c in protocol.cases}
    provider = ArtifactStore(root / "provider")
    ledger = read(".local/P1-16/provider-request-allowance.json")
    if ledger["spec"]["experiment_id"] != identity["experiment_id"]:
        raise ValueError("provider_ledger_for_another_experiment")
    inputs: dict[str, dict[str, dict]] = {}
    for entry in ledger["entries"]:
        if not entry.get("artifact"):
            raise ValueError("pending_provider_request_cannot_seal_review_corpus")
        value = provider.read_json(entry["artifact"]["sha256"])
        pointer = value["input_artifact"]
        raw = provider.read_json(pointer["sha256"])
        if content_hash(raw) != entry["fingerprint"]:
            raise ValueError("provider_input_fingerprint_changed")
        inputs.setdefault(entry["cell"], {})[pointer["sha256"]] = raw
    result = []
    for cell_id, saved in index["rows"].items():
        if any(saved[k] != v for k, v in expected[cell_id].items()):
            raise ValueError("cell_identity_changed")
        case = by_case[saved["case_id"]]
        reference = ReferenceCase.model_validate_json(
            (labels / "references" / (case.case_id + ".json")).read_bytes()
        )
        if content_hash(reference.model_dump(mode="json")) != case.reference_sha256:
            raise ValueError("reference_label_changed")
        observation, report = None, None
        if saved["record"]:
            observation = ArtifactStore(root).read_json(saved["record"]["sha256"])
            if observation["cell"] != expected[cell_id] or observation["state"] != saved["state"]:
                raise ValueError("observation_identity_changed")
            if observation.get("report_reference"):
                pointer = observation["report_reference"]
                report = ArtifactStore(Path(pointer["artifact_root"])).read_json(
                    pointer["report_artifact"]["sha256"]
                )
                if content_hash(report) != observation["report_content_sha256"]:
                    raise ValueError("report_identity_changed")
        result.append(
            {
                "cell": expected[cell_id],
                "state": saved["state"],
                "case": case,
                "reference": reference,
                "observation": observation,
                "report": report,
                "inputs": inputs.get(cell_id, {}),
            }
        )
    return identity, protocol, index, result


def public_packet(row, assessment_id):
    from investigation.flow import redact
    from investigation.review import blind_package

    observation, report, case = row["observation"], row["report"], row["case"]
    if observation is None:
        return None
    if report is None:
        packet = {
            "blind_id": observation["blind_id"],
            "question": case.request.question,
            "scope": case.scope.model_dump(mode="json"),
            "guard": [
                {k: q[k] for k in ["kind", "fields", "prompt"] if k in q}
                for q in observation.get("guard", [])
            ],
            "execution_failed": observation["state"] == "failed",
            "proposal": None,
            "evidence": [],
        }
    else:
        packet, _ = blind_package(report)
        packet["blind_id"] = observation["blind_id"]
        packet["required_context_links"] = context_links(report)
        # Evidence IDs and original reports remain private; aliases suffice for review.
        for link in packet["required_context_links"]:
            link.pop("context_id")
        packet["program_added_context"] = report.get("context_attachments", [])

    def visible_payloads(value):
        if isinstance(value, dict):
            if isinstance(value.get("payload"), dict):
                yield value["payload"]
            for child in value.values():
                yield from visible_payloads(child)
        elif isinstance(value, list):
            for child in value:
                yield from visible_payloads(child)

    seen = [v for raw in row["inputs"].values() for v in visible_payloads(raw["payload"])]
    for evidence in packet["evidence"]:
        text = evidence["payload"].get("text")
        if evidence["kind"] == "text" and isinstance(text, str):
            minimized = redact(text)
            matched = any(
                v.get("text") == minimized and v.get("source_ids") == evidence["source_ids"]
                for v in seen
            )
            evidence["input_visibility"] = {
                "status": "exact_minimized_text_seen" if matched else "not_resolved_by_exact_match",
                "minimized_text": minimized if matched else None,
                "text_differs_from_raw_source": minimized != text if matched else None,
                "not_found_does_not_prove_absence": True,
            }
        elif evidence["kind"] == "structured_lookup":
            expected = redact({k: v for k, v in evidence["payload"].items() if k != "rows"})
            matched = expected.get("source_ids") == evidence["source_ids"] and any(
                v == expected for v in seen
            )
            evidence["input_visibility"] = {
                "status": "exact_minimized_structured_payload_seen"
                if matched
                else "not_resolved_by_exact_match",
                "model_payload": expected if matched else None,
                "raw_fields_not_in_matched_payload": sorted(
                    set(evidence["payload"]) - set(expected)
                )
                if matched
                else None,
                "not_found_does_not_prove_absence": True,
            }
    packet |= {
        "schema_version": "role-aware-blind-packet/0.2",
        "assessment_id": assessment_id,
        "rules": [
            "required_context is a structural role, not proof of semantic support",
            "name the exact evaluated aspect of a compound claim",
            "unreviewed assertions and obligations stay unknown",
            "raw official evidence may differ from minimized model input",
        ],
        "input_visibility_method": "exact_minimized_text_and_source_ids_or_structured_lookup_payload_no_absence_inference",
        "input_coverage": {
            "retained_literal_markers": {
                marker: any(
                    marker in json.dumps(raw, ensure_ascii=False) for raw in row["inputs"].values()
                )
                for marker in ["[NUMBER_REDACTED]", "[VIN_REDACTED]", "[EMAIL_REDACTED]"]
            },
        },
    }
    return packet


def template(row, experiment_id):
    obs, report = row["observation"], row["report"]
    return {
        "schema_version": "role-aware-review/0.2",
        "policy_sha256": content_hash(POLICY),
        "experiment_id": experiment_id,
        "cell_id": row["cell"]["cell_id"],
        "blind_id": obs["blind_id"],
        "observation_sha256": content_hash(obs),
        "report_sha256": content_hash(report) if report is not None else None,
        "reference_sha256": row["case"].reference_sha256,
        "reviewer_kind": None,
        "reviewer_id": "",
        "source_kind": None,
        "sources": [],
        "transcribed_by": "",
        "complete_task_reading": False,
        "task_success": None,
        "disposition_correct": None,
        "dispute_status": "none",
        "citations": [],
        "claims": [],
        "assertions": [],
        "obligations": [],
        "relations": [],
        "assertions_exhaustive": False,
        "input_visibility": None,
        "notes": "UNFILLED. A packet, template or agent draft is not a human judgment.",
    }


def prepare(identity_path, output, source_paths=None):
    identity, protocol, index, rows = load_cells(identity_path)
    registry = {}
    defaults = [
        "P1-16-human-review-C001.json",
        "P1-16-human-review-C002-C004.json",
        "P1-16-human-review-complete.json",
    ]
    for path in source_paths or [Path("docs/experiments") / name for name in defaults]:
        path = Path(path)
        if read(path).get("reviewer_kind") != "human":
            raise ValueError("trusted_human_source_must_record_actual_human_provenance")
        registry[path.as_posix()] = read(path)
    manifest = {
        "schema_version": "role-aware-corpus/0.2",
        "policy": POLICY,
        "policy_sha256": content_hash(POLICY),
        "analysis_implementation": implementation(),
        "frozen_experiment_id": identity["experiment_id"],
        "run_index_content_sha256": content_hash(index),
        "cells": [
            {
                "cell": r["cell"],
                "state": r["state"],
                "observation_sha256": content_hash(r["observation"]) if r["observation"] else None,
                "report_sha256": content_hash(r["report"]) if r["report"] is not None else None,
                "reference_sha256": r["case"].reference_sha256,
                "provider_input_sha256": sorted(r["inputs"]),
            }
            for r in rows
        ],
        "trusted_human_sources": {k: content_hash(v) for k, v in registry.items()},
        "source_trust_basis": "actual user replies already archived; review files cannot self-register human provenance",
        "quality_target_status": "pending_independent_development_baseline_no_posthoc_threshold",
    }
    assessment_id = content_hash(manifest)
    directory = output / assessment_id
    write_new(directory / "manifest.json", manifest)
    write_new(directory / "source-registry.json", registry)
    for name, digest in manifest["analysis_implementation"].items():
        data = Path(name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("analysis_code_changed_during_prepare")
        snapshot = directory / "analysis-source" / name
        native_path(snapshot.parent).mkdir(parents=True, exist_ok=True)
        if native_path(snapshot).exists():
            if native_path(snapshot).read_bytes() != data:
                raise ValueError("analysis_source_snapshot_changed")
        else:
            with native_path(snapshot).open("xb") as stream:
                stream.write(data)
    for row in rows:
        packet = public_packet(row, assessment_id)
        if packet is None:
            continue
        blind = packet["blind_id"]
        write_new(directory / "packets" / (blind + ".json"), packet)
        write_new(
            directory / "templates-private" / (blind + ".json"),
            template(row, identity["experiment_id"]),
        )
    pointer = {
        "assessment_id": assessment_id,
        "directory": directory.as_posix(),
        "identity_path": Path(identity_path).as_posix(),
        "manifest_sha256": content_hash(manifest),
    }
    write_new(output / (assessment_id + ".json"), pointer)
    return pointer


def verified(pointer):
    directory = Path(pointer["directory"])
    manifest = read(directory / "manifest.json")
    if (
        content_hash(manifest) != pointer["assessment_id"]
        or content_hash(manifest) != pointer["manifest_sha256"]
        or manifest["policy"] != POLICY
        or manifest["analysis_implementation"] != implementation()
    ):
        raise ValueError("assessment_policy_code_or_manifest_changed_new_revision_required")
    identity, protocol, index, rows = load_cells(pointer["identity_path"])
    if content_hash(index) != manifest["run_index_content_sha256"]:
        raise ValueError("frozen_run_index_changed_after_review_seal")
    for row, saved in zip(rows, manifest["cells"], strict=True):
        if (
            row["cell"] != saved["cell"]
            or (content_hash(row["observation"]) if row["observation"] else None)
            != saved["observation_sha256"]
            or (content_hash(row["report"]) if row["report"] is not None else None)
            != saved["report_sha256"]
            or row["case"].reference_sha256 != saved["reference_sha256"]
            or sorted(row["inputs"]) != saved["provider_input_sha256"]
        ):
            raise ValueError("sealed_review_cell_binding_changed")
    registry = read(directory / "source-registry.json")
    if {k: content_hash(v) for k, v in registry.items()} != manifest["trusted_human_sources"]:
        raise ValueError("trusted_source_registry_changed")
    for name, digest in manifest["analysis_implementation"].items():
        if (
            hashlib.sha256(
                native_path(directory / "analysis-source" / name).read_bytes()
            ).hexdigest()
            != digest
        ):
            raise ValueError("archived_analysis_source_changed")
    return directory, identity, protocol, rows, registry


def import_review(pointer, value):
    directory, identity, protocol, rows, registry = verified(pointer)
    row = next((r for r in rows if r["cell"]["cell_id"] == value["cell_id"]), None)
    if row is None or row["observation"] is None:
        raise ValueError("cannot_review_unobserved_cell")
    result = assess(
        row["observation"],
        row["report"],
        row["reference"],
        row["case"],
        value,
        experiment_id=identity["experiment_id"],
        sources=registry,
        inputs=row["inputs"],
    )
    saved = {"review": value, "result": result}
    path = directory / "reviews" / (content_hash(saved) + ".json")
    write_new(path, saved)
    # Multiple human reviews must be adjudicated, never silently last-write-wins.
    index_path = directory / "review-index.json"
    from evaluation.records import atomic_json, exclusive

    with exclusive(native_path(index_path)):
        index = read(index_path) if native_path(index_path).exists() else {}
        entries = index.setdefault(value["cell_id"], [])
        if path.stem not in entries:
            entries.append(path.stem)
            atomic_json(native_path(index_path), index)
    return result


def arm_metrics(group):
    judged = [r for r in group if r["human_task_judgment"] is not None]
    formal = [r["formal"] for r in group if r["formal"] is not None]
    citations_ready = len(formal) == len(group) and all(
        m["citation_roles_complete"] for m in formal
    )
    direct_n = sum(m["direct_citation_pairs"] for m in formal)
    direct_supported = sum(m["direct_citation_verdict_counts"].get("supported", 0) for m in formal)
    assertions_ready = len(formal) == len(group) and all(
        m["atomic_assertions_exhaustive"] or m["citation_pairs_expected"] == 0 for m in formal
    )
    atomic_n = sum(m["reviewed_atomic_assertion_count"] for m in formal)
    return {
        "denominator": len(group),
        "human_task_judgments": len(judged),
        "reviewed_successes": sum(r["human_task_judgment"] is True for r in judged),
        "task_completion_rate": sum(r["human_task_judgment"] is True for r in judged) / len(group)
        if len(judged) == len(group)
        else None,
        "citation_role_reviewed_cells": sum(m["citation_roles_complete"] for m in formal),
        "direct_citation_aspect_support_rate": direct_supported / direct_n
        if citations_ready and direct_n
        else None,
        "atomic_reviewed_cells": sum(m["atomic_assertions_exhaustive"] for m in formal),
        "unsupported_atomic_assertion_rate": sum(
            m["unsupported_atomic_assertion_count"] for m in formal
        )
        / atomic_n
        if assertions_ready and atomic_n
        else None,
        "partial_atomic_assertion_rate": sum(m["partial_atomic_assertion_count"] for m in formal)
        / atomic_n
        if assertions_ready and atomic_n
        else None,
    }


def analyze(pointer):
    directory, identity, protocol, rows, registry = verified(pointer)
    index = (
        read(directory / "review-index.json")
        if native_path(directory / "review-index.json").exists()
        else {}
    )
    if not set(index) <= {r["cell"]["cell_id"] for r in rows} or any(
        len(v) != len(set(v)) for v in index.values()
    ):
        raise ValueError("unknown_or_duplicate_review_index_entry")
    assessed = []
    for row in rows:
        imported = []
        for digest in index.get(row["cell"]["cell_id"], []):
            value = read(directory / "reviews" / (digest + ".json"))
            if content_hash(value) != digest:
                raise ValueError("imported_review_changed")
            result = assess(
                row["observation"],
                row["report"],
                row["reference"],
                row["case"],
                value["review"],
                experiment_id=identity["experiment_id"],
                sources=registry,
                inputs=row["inputs"],
            )
            if result != value["result"]:
                raise ValueError("review_score_changed")
            imported.append(result)
        humans = [r for r in imported if r["reviewer_kind"] == "human"]
        formal = humans[0]["formal"] if len(humans) == 1 else None
        assessed.append(
            {
                "cell": row["cell"],
                "state": row["state"],
                "human_review_conflict": len(humans) > 1,
                "human_task_judgment": formal["task_success"] if formal is not None else None,
                "formal": formal,
                "imported_reviews": len(imported),
            }
        )
    arms = {}
    for arm in protocol.arms:
        group = [r for r in assessed if r["cell"]["arm_id"] == arm.arm_id]
        arms[arm.arm_id] = arm_metrics(group)
    return {
        "schema_version": "role-aware-summary/0.2",
        "assessment_id": pointer["assessment_id"],
        "experiment_id": identity["experiment_id"],
        "post_hoc_revision": True,
        "frozen_denominator": len(assessed),
        "arms": arms,
        "rows": assessed,
        "human_task_judgments": sum(r["human_task_judgment"] is not None for r in assessed),
        "aggregate_quality_metrics": arms
        if all(a["human_task_judgments"] == a["denominator"] for a in arms.values())
        else None,
        "new_provider_requests": 0,
        "limits": [
            "One retained human judgment does not estimate population quality.",
            "Whole-claim, atomic-assertion, citation-aspect, obligation and task metrics have separate coverage.",
            "This policy was revised after observing test outcomes; no pre-registered quality benefit is inferred.",
        ],
    }

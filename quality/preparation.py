"""Freeze minimized public-evidence judge inputs; human labels never enter prompts."""

import hashlib
import json
from pathlib import Path

from ingestion.records import content_hash
from quality.prompt import SYSTEM
from reviewing.corpus import load_cells, public_packet, read, write_new
from runtime.artifacts import ArtifactStore


def implementation():
    return {
        p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path("quality").glob("*.py"))
    }


def source_units(references):
    from releases.bundle import BundleStore

    plan = read("evals/development-baseline-v0.1/prepared-plan.json")
    store = BundleStore(Path(".local/releases"))
    manifest = store.manifests.read_json(plan["manifest_sha256"])
    base = store.directory(manifest)
    corpus = ArtifactStore(base / "index").read_json(manifest["index_artifact_sha256"])["corpus"]
    needed = {
        a["unit_id"] for ref in references for slot in ref["slots"] for a in slot["alternatives"]
    }
    result = {}
    for digest in sorted({d["parse_artifact_sha256"] for d in corpus["documents"]}):
        parsed = ArtifactStore(base / "parsed").read_json(digest)
        for unit in parsed["units"]:
            if unit["unit_id"] in needed:
                if parsed["source"]["source_id"].startswith("NHTSA-COMPLAINTS"):
                    raise ValueError("complete_complaints_are_outside_review_proposal")
                result[unit["unit_id"]] = {
                    "source_id": parsed["source"]["source_id"],
                    "source_version_id": parsed["source"]["source_version_id"],
                    "unit_id": unit["unit_id"],
                    "field": unit["field_pointer"],
                    "page": unit["page_number"],
                    "text": unit["text"],
                }
    if not needed <= result.keys():
        raise ValueError("reference_source_unit_missing")
    return result


def minimize(packet, reference, units, observation, category):
    evidence, grouped = [], {}
    for card in packet["evidence"]:
        # Exact whole payloads are grouped; aliases, provenance and context remain explicit.
        payload = card["payload"]
        if card["kind"] == "text":
            payload = {"text": payload["text"]}
        value = {
            "kind": card["kind"],
            "scope_channel": card["scope_channel"],
            "source_ids": card["source_ids"],
            "payload": payload,
        }
        key = content_hash(value)
        if key not in grouped:
            grouped[key] = value | {"aliases": [], "input_visibility": []}
            evidence.append(grouped[key])
        grouped[key]["aliases"].append(card["alias"])
        grouped[key]["input_visibility"].append(card.get("input_visibility"))
    source_groups = {}
    for slot in reference["slots"]:
        for alt in slot["alternatives"]:
            unit = units[alt["unit_id"]]
            key = content_hash({k: unit[k] for k in ("source_id", "field", "page", "text")})
            if key not in source_groups:
                source_groups[key] = unit | {"slot_ids": []}
            if slot["slot_id"] not in source_groups[key]["slot_ids"]:
                source_groups[key]["slot_ids"].append(slot["slot_id"])
    context = sorted(
        {
            (r["claim_id"], a)
            for r in packet.get("required_context_links", [])
            for a in r["context_aliases"]
        }
    )
    return {
        "blind_id": packet["blind_id"],
        "question": packet["question"],
        "scope": packet["scope"],
        "time": packet.get("time"),
        "proposal": packet.get("proposal"),
        "guard": [
            {k: q[k] for k in ("kind", "fields", "prompt") if k in q}
            for q in observation.get("guard") or []
        ],
        "execution_state": observation["state"],
        "task_category": category,
        "execution_failed": packet["execution_failed"],
        "evidence_groups": evidence,
        "reference_sources": list(source_groups.values()),
        "obligations": [
            {
                "obligation_id": c["reference_claim_id"],
                "statement": c["statement"],
                "forbidden_inferences": c["forbidden_inferences"],
                "evidence_sets": c["evidence_sets"],
            }
            for c in reference["required_claims"]
        ],
        "program_context_pairs": context,
        "required_relation_types": (
            ["excluded_by_scope", "applicability_unknown"]
            if category == "wrong_relation"
            else ["scope_overlap", "applicability_unknown"]
            if "同一缺陷" in packet["question"]
            else []
        ),
        "rules": [
            "reference labels are implementation-authored and must be checked against sources",
            "input visibility is for attribution only, not a semantic grade",
            "no human grades or method identities supplied",
        ],
    }


def prepare(root=Path(".local/q16")):
    _, _, _, old = load_cells(Path(".local/P1-16/active-experiment.json"))
    dev_summary = read("docs/verification/2026-10-29-P1-16-development-review-complete.json")
    dev_plan = read("evals/development-baseline-v0.1/prepared-plan.json")
    units = source_units(
        [r["reference"].model_dump(mode="json") for r in old]
        + list(dev_plan["references"].values())
    )
    jobs, absent = [], []
    inputs = ArtifactStore(root / "inputs")
    for row in dev_summary["rows"]:
        packet = read(row["review_packet_path"])
        if content_hash(packet) != row["review_packet_content_sha256"]:
            raise ValueError("development_reading_packet_changed")
        ref = dev_plan["references"][row["cell"]["case_id"]]
        case = next(c for c in dev_plan["cases"] if c["case_id"] == row["cell"]["case_id"])
        payload = minimize(packet, ref, units, row, case["category"])
        jobs.append(
            {
                "job_id": "DEV-" + packet["blind_id"],
                "cohort": "calibration",
                "cell": row["cell"],
                "input": inputs.put_json(payload),
                "packet_sha256": content_hash(packet),
                "reference_sha256": content_hash(ref),
            }
        )
    for row in old:
        if row["observation"] is None:
            absent.append({"cell": row["cell"], "state": row["state"], "semantic_judgment": None})
            continue
        packet = public_packet(row, "model-assisted-preparation")
        payload = minimize(
            packet,
            row["reference"].model_dump(mode="json"),
            units,
            row["observation"],
            row["case"].category,
        )
        calibration = packet["blind_id"] == "BLIND-525c27d5cddc"
        jobs.append(
            {
                "job_id": "OLD-" + packet["blind_id"],
                "cohort": "calibration_historical_negative"
                if calibration
                else "posthoc_population",
                "cell": row["cell"],
                "input": inputs.put_json(payload),
                "observation_sha256": content_hash(row["observation"]),
                "report_sha256": content_hash(row["report"]) if row["report"] else None,
                "reference_sha256": row["case"].reference_sha256,
            }
        )
    lengths = {
        j["job_id"]: len(
            SYSTEM + json.dumps(inputs.read_json(j["input"]["sha256"]), ensure_ascii=False)
        )
        for j in jobs
    }
    if max(lengths.values()) > 160000:
        raise ValueError("review_input_above_declared_character_cap")
    plan = {
        "schema_version": "quality-review-plan/0.1",
        "classification": "model_assisted_not_human",
        "model": "deepseek-v4-pro",
        "generator_model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com",
        "same_vendor_independence_limit": True,
        "system": SYSTEM,
        "implementation": implementation(),
        "jobs": jobs,
        "missing_observations": absent,
        "input_store": (root / "inputs").as_posix(),
        "per_request_completion_tokens": 8192,
        "per_input_character_cap": 160000,
        "maximum_input_characters_observed": max(lengths.values()),
        "approval": "none_yet_no_provider_request",
        "human_grades_exposed_to_model": False,
        "calibration_jobs": [j["job_id"] for j in jobs if j["cohort"].startswith("calibration")],
        "population_purpose": "post_hoc_measurement_of_old_270_cells_never_retroactive_preregistration",
    }
    plan_id = content_hash(plan)
    pointer = {"plan_id": plan_id, "path": (root / (plan_id + ".json")).as_posix()}
    write_new(Path(pointer["path"]), plan)
    write_new(root / "active-review-plan.json", pointer)
    return pointer, plan, lengths

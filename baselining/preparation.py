"""Pin a small seen-development pilot without changing frozen test code or labels."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from evaluation.contracts import Case
from evaluation.freeze import implementation as generator_implementation
from evaluation.freeze import load
from ingestion.records import content_hash
from investigation.contracts import FixedConfig, ReferenceCase
from parsing.parser import text_hash
from releases.bundle import BundleStore
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore

ROOT = Path("evals/development-baseline-v0.1")
RELEASE = "035708451b144f0835e15fe940ab7e0c9eb7bfc168f4baba9dd379d1f43c2c1f"
SOURCES = {
    "prius": "NHTSA-API-CAMPAIGN-10V039000",
    "investigation": "NHTSA-INV-PE10006-CLOSE",
    "cover": "NHTSA-API-CAMPAIGN-17V331000",
    "cable": "NHTSA-API-CAMPAIGN-17V652000",
}


def wrapper_implementation():
    return {
        p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path("baselining").glob("*.py"))
    }


def validate_design(design):
    if (
        design["split"] != "development"
        or design["method"] != "fixed"
        or design["memory"] != "none"
        or design["model"] != "deepseek-flash"
        or design["endpoint"] != "https://api.deepseek.com"
        or design["repeats"] != 1
        or design["quality_targets"] is not None
        or set(design["allowed_source_ids"]) != set(SOURCES.values())
    ):
        raise ValueError("pilot_scope_or_unestablished_quality_target_changed")
    rows = design["cases"]
    if len(rows) != 8 or len({r["case_id"] for r in rows}) != 8:
        raise ValueError("pilot_requires_eight_distinct_development_tasks")
    for row in rows:
        if (
            not set(row["source_keys"]) <= SOURCES.keys()
            or not row["source_keys"]
            or row["event_cluster_id"] not in design["event_clusters"]
            or type(row["maximum_requests"]) is not int
            or row["maximum_requests"] not in {0, 1}
            or (row["maximum_requests"] == 1)
            != (row["expected_preview"] == "stopped_before_provider")
        ):
            raise ValueError("pilot_task_source_or_request_cap_invalid")
    if sum(r["maximum_requests"] for r in rows) != design["maximum_requests"]:
        raise ValueError("pilot_total_request_cap_changed")
    if design["maximum_requests"] != 3:
        raise ValueError("pilot_is_exactly_three_potential_provider_requests")
    return design


def locate_atom(atom, parsed):
    matches = []
    for document in parsed:
        if document["source"]["source_id"] != SOURCES[atom["source"]]:
            continue
        # All same-text vehicle rows are legitimate alternatives, never filtered by matched.
        for unit in document["units"]:
            if atom["field"] and not (unit["field_pointer"] or "").endswith("/" + atom["field"]):
                continue
            if atom["quote"] not in unit["text"]:
                continue
            start = unit["text"].index(atom["quote"])
            matches.append(
                {
                    "source_version_id": document["source"]["source_version_id"],
                    "unit_id": unit["unit_id"],
                    "start": start,
                    "end": start + len(atom["quote"]),
                    "quote_sha256": text_hash(atom["quote"]),
                }
            )
    if not matches:
        raise ValueError("development_quote_not_found_in_original_source:" + atom["key"])
    return matches


def prepare(output=Path(".local/P1-16/development-baseline")):
    design = validate_design(read(ROOT / "design.json"))
    atom_spec = read(ROOT / "atoms.json")
    if atom_spec["source_ids"] != SOURCES:
        raise ValueError("development_atom_sources_changed")
    body, original = load(read(".local/P1-16/active-experiment.json"))
    if original.dataset_release_id != RELEASE:
        raise ValueError("unexpected_pilot_release")
    store = BundleStore(Path(".local/releases"))
    manifest = store.manifests.read_json(original.manifest_sha256)
    base = store.directory(manifest)
    corpus = ArtifactStore(base / "index").read_json(manifest["index_artifact_sha256"])["corpus"]
    digests = sorted(
        {
            d["parse_artifact_sha256"]
            for d in corpus["documents"]
            if d["metadata"]["source_id"] in SOURCES.values()
        }
    )
    parsed = [ArtifactStore(base / "parsed").read_json(d) for d in digests]
    atoms = {a["key"]: a for a in atom_spec["atoms"]}
    if len(atoms) != len(atom_spec["atoms"]):
        raise ValueError("duplicate_development_obligation")
    slots = {
        key: {"slot_id": key, "kind": "text", "alternatives": locate_atom(atom, parsed)}
        for key, atom in atoms.items()
    }
    references, cases = {}, []
    for row in design["cases"]:
        reference = ReferenceCase.model_validate(
            {
                "case_id": row["case_id"],
                "split": "development",
                "slots": [slots[k] for k in row["obligation_keys"]],
                "required_claims": [
                    {
                        "reference_claim_id": "R-" + k.upper().replace("_", "-"),
                        "statement": atoms[k]["statement"],
                        "evidence_sets": [[k]],
                        "forbidden_inferences": ["individual_applicability", "all_vehicles_safe"],
                    }
                    for k in row["obligation_keys"]
                ],
                "acceptable_dispositions": [row["expected_disposition"]],
            }
        ).model_dump(mode="json")
        references[row["case_id"]] = reference
        request = {
            "task_id": row["case_id"],
            "intent": row["intent"],
            "question": row["question"],
            "retrieval_query": row["query"],
            "expected_release_id": RELEASE,
        } | {
            k: row[k]
            for k in ("pairs", "expected_time", "required_conditions", "components")
            if k in row
        }
        case = Case.model_validate(
            {
                "case_id": row["case_id"],
                "event_cluster_id": row["event_cluster_id"],
                "split": "development",
                "category": "direct" if row["intent"] == "record_lookup" else "cross_source",
                "sources": [SOURCES[k] for k in row["source_keys"]],
                "scope": row["scope"],
                "request": request,
                "reference_sha256": content_hash(reference),
            }
        )
        cases.append(case.model_dump(mode="json"))
    plan = {
        "schema_version": "development-baseline-plan/0.1",
        "classification": "seen_development_pilot_not_new_test_or_quality_acceptance",
        "design": design,
        "atom_spec": atom_spec,
        "profile": original.profile.model_dump(mode="json"),
        "dataset_release_id": RELEASE,
        "manifest_sha256": original.manifest_sha256,
        "cases": cases,
        "references": references,
        "parsed_artifact_sha256": digests,
        "generator_implementation": body["implementation_sha256"],
        "wrapper_implementation": wrapper_implementation(),
        "provider_approval": "pending_actual_human_reply",
    }
    plan_id = content_hash(plan)
    root = output / plan_id
    write_new(root / "plan.json", plan)
    for key, value in references.items():
        write_new(root / "references" / (key + ".json"), value)
    pointer = {"plan_id": plan_id, "directory": root.as_posix()}
    write_new(output / (plan_id + ".json"), pointer)
    return pointer


def checked(pointer):
    plan = read(Path(pointer["directory"]) / "plan.json")
    if (
        content_hash(plan) != pointer["plan_id"]
        or plan["generator_implementation"] != generator_implementation()
        or plan["wrapper_implementation"] != wrapper_implementation()
    ):
        raise ValueError("pilot_plan_or_code_changed_new_prepare_required")
    validate_design(plan["design"])
    cases = [Case.model_validate(c) for c in plan["cases"]]
    for case in cases:
        ref = plan["references"][case.case_id]
        if (
            content_hash(ref) != case.reference_sha256
            or read(Path(pointer["directory"]) / "references" / (case.case_id + ".json")) != ref
        ):
            raise ValueError("pilot_reference_changed")
    protocol = SimpleNamespace(
        cases=cases,
        arms=[
            SimpleNamespace(
                arm_id="fixed",
                method="fixed",
                family="main",
                memory="none",
                retrieval=plan["profile"]["retrieval_method"],
                context=plan["profile"]["context_mode"],
            )
        ],
        profile=FixedConfig.model_validate(plan["profile"]),
        dataset_release_id=plan["dataset_release_id"],
    )
    cells = [
        {
            "cell_id": "fixed:" + c.case_id + ":1",
            "arm_id": "fixed",
            "case_id": c.case_id,
            "repeat": 1,
            "family": "main",
            "method": "fixed",
            "event_cluster_id": c.event_cluster_id,
        }
        for c in cases
    ]
    return plan, protocol, cells

"""Prepare a new, unrun quality-confirmation candidate; targets stay pending human labels."""

import argparse
from pathlib import Path

from domain.tasks import TimeScope
from entities.normalize import EntityRules, lexical
from entities.scopes import load_scope_rules
from ingestion.contracts import Snapshot
from ingestion.records import content_hash, prepare_import
from investigation.contracts import ReferenceCase
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from parsing.parser import text_hash
from releases.bundle import BundleStore
from retrieval.contracts import BuildRequest, RetrievalConfig
from retrieval.corpus import build_corpus
from retrieval.index import build_index, checked_index
from retrieval.models import LocalModel
from reviewing.corpus import read, write_new
from runtime.artifacts import ArtifactStore
from scripts.prepare_quality_sources import GROUPS

VEHICLES = {
    "Q-HYUNDAI-LFB": ("HYUNDAI", "IONIQ 5", 2025),
    "Q-TESLA-Y": ("TESLA", "MODEL Y", 2025),
    "Q-TACOMA": ("TOYOTA", "TACOMA", 2025),
    "Q-HONDA-STEERING": ("HONDA", "CR-V", 2025),
    "Q-BMW-BRAKE": ("BMW", "X5", 2025),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_root
    root.mkdir(parents=True, exist_ok=True)
    receipts = read(args.source_root / "receipts.json")["snapshots"]
    source_root = args.source_root / "raw"
    # Register literal source field values through a fixed algorithm, without answer labels,
    # aliases, free-text extraction, source filtering, or old-release mutation.
    rules = read("examples/retrieval-entities.json")
    rules["rules_version"] = "quality-literal-source-fields/0.1"
    all_rows = {}
    by_code = {}
    for raw in receipts:
        snap = Snapshot.model_validate(raw)
        batch = prepare_import(source_root, snap.snapshot_id)
        if any(r.version is None for r in batch.rows):
            raise ValueError("confirmation_raw_row_not_importable")
        code = snap.source.selection["campaign_number"]
        by_code[code] = raw
        rows = []
        for r in batch.rows:
            row = r.version["payload"]
            rows.append(row)
            make, model = lexical(row["Make"]), lexical(row["Model"])
            if make in rules["unknown_labels"] or model in rules["unknown_labels"]:
                raise ValueError("source_unknown_label_not_silently_canonicalized")
            rules["models_by_make"].setdefault(make, [])
            if model not in rules["models_by_make"][make]:
                rules["models_by_make"][make].append(model)
            component = lexical(row["Component"])
            if component not in rules["component_labels"]:
                rules["component_labels"].append(component)
        all_rows[code] = rows
    rules["models_by_make"] = {k: sorted(v) for k, v in sorted(rules["models_by_make"].items())}
    rules["component_labels"] = sorted(rules["component_labels"])
    entity = EntityRules.model_validate(rules)
    write_new(root / "literal-entity-rules.json", rules)
    provenance = {
        "method": "register_all_literal_structured_fields_before_any_generation_not_from_gold_or_model_outputs",
        "source_receipts_sha256": content_hash(receipts),
        "rules_sha256": entity.sha256,
        "old_rules_changed": False,
        "fields_added_to_raw_records": False,
    }
    write_new(root / "literal-entity-provenance.json", provenance)
    scope = load_scope_rules().model_copy(
        update={"entity_rules": entity, "rules_version": "quality-literal-scope-entities/0.1"}
    )
    pdf = {"documents": []}
    rule_bundle = {
        "entity": rules,
        "scope": scope.model_dump(mode="json"),
        "pdf": pdf,
        "chunk": load_chunk_config("structure").model_dump(mode="json"),
        "structure": load_structure_rules().model_dump(mode="json"),
    }
    index_pointer = root / "index-pointer.json"
    if index_pointer.exists():
        ref = read(index_pointer)
        index = checked_index(root / "index", ref["sha256"])
        corpus = index["corpus"]
    else:
        corpus = build_corpus(
            source_root,
            root / "parsed",
            root / "chunks",
            BuildRequest(
                snapshot_ids=[r["snapshot_id"] for r in receipts],
                time=TimeScope(
                    mode="snapshot_analysis",
                    event_interval=None,
                    knowledge_cutoff=None,
                    date_filter_basis="actual frozen confirmation source receipts; no historical reconstruction",
                ),
            ),
            entity,
            scope,
            pdf,
        )
        index = build_index(corpus, LocalModel("encoder"), RetrievalConfig())
        ref = ArtifactStore(root / "index").put_json(index)
        write_new(index_pointer, ref)
    store = BundleStore(root / "releases")
    sealed = store.create(
        root / "index",
        ref["sha256"],
        raw_root=source_root,
        parsed_root=root / "parsed",
        chunk_root=root / "chunks",
        model_root=Path(".local/retrieval/models"),
        encoder_identity=LocalModel("encoder").identity,
        reranker_identity=LocalModel("reranker").identity,
        rules=rule_bundle,
    )
    cases = []
    references = {}
    for group, codes in GROUPS.items():
        make, model, year = VEHICLES[group]
        primary = codes[0]
        ordinals = {
            code: next(
                i
                for i, r in enumerate(all_rows[code])
                if lexical(r["Make"]) == make
                and lexical(r["Model"]) == model
                and int(r["ModelYear"]) == year
            )
            for code in codes
        }
        slots = []
        obligations = []
        for code in codes:
            documents = [
                d
                for d in corpus["documents"]
                if d["metadata"]["source_id"] == by_code[code]["source"]["source_id"]
                and d["source"]["ordinal"] == ordinals[code]
            ]
            parsed = ArtifactStore(root / "parsed").read_json(documents[0]["parse_artifact_sha256"])
            for field in ("Summary", "Consequence", "Remedy"):
                unit = next(u for u in parsed["units"] if u["field_pointer"].endswith("/" + field))
                key = code + "-" + field.lower()
                slots.append(
                    {
                        "slot_id": key,
                        "kind": "text",
                        "alternatives": [
                            {
                                "source_version_id": parsed["source"]["source_version_id"],
                                "unit_id": unit["unit_id"],
                                "start": 0,
                                "end": len(unit["text"]),
                                "quote_sha256": text_hash(unit["text"]),
                            }
                        ],
                    }
                )
                obligations.append(
                    {
                        "reference_claim_id": "R-" + code + "-" + field.upper(),
                        "statement": unit["text"],
                        "evidence_sets": [[key]],
                        "forbidden_inferences": [
                            "individual_applicability",
                            "causality",
                            "incidence_rate",
                            "all_vehicles_safe",
                        ],
                    }
                )
        for suffix, category in [
            ("FACTS", "direct"),
            ("COUNTER", "counterevidence"),
            ("PAIR", "wrong_relation" if len(codes) > 1 else "cross_source"),
            ("CONDITIONS", "missing_conditions"),
            ("YEAR", "scope_exclusion"),
            ("TIME", "time_boundary"),
            ("INSUFFICIENT", "insufficient"),
        ]:
            identifier = "QTEST-" + primary + "-" + suffix
            scope_body = {
                "make": make,
                "model": model,
                "model_year": year,
                "campaign_number": primary,
            }
            allowed = [by_code[primary]["source"]["source_id"]]
            req = {
                "task_id": identifier,
                "intent": "record_lookup",
                "retrieval_query": " ".join(codes) + " summary consequence remedy conditions",
                "question": "依据允许的冻结记录，说明该车型范围内缺陷、触发条件、风险与免费补救；保留certain范围，不确认个车适用、不推算故障率。",
            }
            chosen_slots = [s for s in slots if s["slot_id"].startswith(primary)]
            chosen_ob = [
                o for o in obligations if o["reference_claim_id"].startswith("R-" + primary)
            ]
            dispositions = ["answered"]
            if suffix == "COUNTER":
                req["question"] += (
                    " 有人声称完成召回证明所有同品牌车辆永远安全，请直接核对该推断并说明证据边界。"
                )
                chosen_slots += [
                    {
                        "slot_id": "bounded-scope",
                        "kind": "structured_lookup",
                        "expected_fields": {
                            "coverage": "all_selected_versions_in_allowed_frozen_sources_only"
                        },
                    }
                ]
                chosen_ob += [
                    {
                        "reference_claim_id": "R-NO-PERMANENT-SAFETY",
                        "statement": "明确否定全品牌永久安全外推；记录仅涉及特定范围、缺陷与快照。",
                        "evidence_sets": [
                            [primary + "-summary", primary + "-remedy", "bounded-scope"]
                        ],
                        "forbidden_inferences": ["all_vehicles_safe"],
                    }
                ]
            elif suffix == "PAIR" and len(codes) > 1:
                allowed = [by_code[c]["source"]["source_id"] for c in codes]
                scope_body.pop("campaign_number")
                req |= {
                    "intent": "relationship",
                    "question": "对照两条允许来源，分别保留适用范围、触发条件、缺陷、风险和补救。共享车型或部件是否证明同一缺陷或个车适用？区分范围重叠、相似症状、官方引用、排除和未知。",
                    "pairs": [
                        {
                            "left": {
                                "snapshot_id": by_code[codes[0]]["snapshot_id"],
                                "ordinal": ordinals[codes[0]],
                            },
                            "right": {
                                "snapshot_id": by_code[codes[1]]["snapshot_id"],
                                "ordinal": ordinals[codes[1]],
                            },
                        }
                    ],
                }
                chosen_slots = slots + [{"slot_id": "typed-relation", "kind": "relationship"}]
                chosen_ob = obligations + [
                    {
                        "reference_claim_id": "R-RELATION-BOUNDARY",
                        "statement": "两来源机制/条件分别核对，关系类型与未知保留；不能由车型或部件外推同一缺陷/个车适用。",
                        "evidence_sets": [["typed-relation"]],
                        "forbidden_inferences": ["individual_applicability", "causality"],
                    }
                ]
            elif suffix == "PAIR":
                req["intent"] = "cross_source"
                req["question"] = (
                    "仅允许此召回来源，能否提供该事件的调查结案依据及结案后的继续监测安排？缺调查材料时明确不足，不得编造。"
                )
                chosen_slots = []
                chosen_ob = []
                dispositions = ["insufficient_evidence"]
            elif suffix == "CONDITIONS":
                req |= {
                    "intent": "applicability",
                    "question": "没有个车生产日期和配置，能否确认我的车适用并且召回后永久安全？先明确缺失条件。",
                }
                dispositions = ["needs_clarification"]
                chosen_slots = []
                chosen_ob = []
            elif suffix == "YEAR":
                scope_body["model_year"] = (
                    min(
                        int(r["ModelYear"])
                        for r in all_rows[primary]
                        if lexical(r["Make"]) == make and lexical(r["Model"]) == model
                    )
                    - 1
                )
                req["question"] = (
                    "按当前年份查询允许来源；零匹配不能说明全球无召回，不得用别的年份替换。"
                )
                dispositions = ["no_matching_records"]
                chosen_slots = []
                chosen_ob = []
            elif suffix == "TIME":
                req |= {
                    "question": "只使用2010年1月1日当时可知的资料；没有版本公开时间证据时停止澄清，不能用后来冻结快照替代。",
                    "expected_time": {
                        "mode": "historical_as_of",
                        "event_interval": None,
                        "knowledge_cutoff": "2010-01-01T00:00:00Z",
                        "date_filter_basis": "no version-publication proof",
                    },
                }
                dispositions = ["unsupported_time", "needs_clarification"]
                chosen_slots = []
                chosen_ob = []
            elif suffix == "INSUFFICIENT":
                req |= {
                    "intent": "complaint_statistics",
                    "question": "仅依据允许的召回记录给出投诉总数和故障发生率；若缺投诉全集和暴露量应说明不足，不能编造。",
                }
                dispositions = ["insufficient_evidence"]
                chosen_slots = []
                chosen_ob = []
            reference = ReferenceCase.model_validate(
                {
                    "case_id": identifier,
                    "split": "test",
                    "slots": chosen_slots,
                    "required_claims": chosen_ob,
                    "acceptable_dispositions": dispositions,
                }
            )
            references[identifier] = reference.model_dump(mode="json")
            cases.append(
                {
                    "case_id": identifier,
                    "event_cluster_id": group,
                    "split": "test",
                    "category": category,
                    "sources": allowed,
                    "scope": scope_body,
                    "request": req,
                    "reference_sha256": content_hash(references[identifier]),
                }
            )
    candidate = {
        "schema_version": "quality-confirmation-candidate/0.1",
        "status": "not_frozen_pending_human_development_components_and_sampling",
        "release": sealed,
        "release_root": store.root.as_posix(),
        "sources": receipts,
        "groups": GROUPS,
        "cases": cases,
        "references": references,
        "quality_targets": None,
        "model_requests": 0,
        "literal_dictionary_provenance": provenance,
        "not_an_executed_quality_trial": True,
        "previous_270_cells_not_reclassified": True,
    }
    write_new(root / "candidate.json", candidate)
    print(
        {
            "candidate": (root / "candidate.json").as_posix(),
            "cases": len(cases),
            "clusters": len(GROUPS),
            "generation_requests": 0,
            "release": sealed["release_id"],
        }
    )


if __name__ == "__main__":
    main()

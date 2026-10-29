"""Build new official evaluation inputs and references before any model output is inspected."""

import argparse
import json
from pathlib import Path

from entities.normalize import EntityRules
from entities.scopes import load_scope_rules
from evaluation.contracts import Protocol
from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from parsing.chunk_contracts import load_chunk_config, load_structure_rules
from parsing.parser import text_hash
from releases.bundle import BundleStore, read_json
from retrieval.contracts import BuildRequest, RetrievalConfig
from retrieval.corpus import build_corpus
from retrieval.index import build_index, checked_index
from retrieval.models import LocalModel
from runtime.artifacts import ArtifactStore

ROOT = Path("evals/comprehensive-v0.1")
PAIRS = [
    ["14V047000", "15V461000"],
    ["18V332000", "19V053000"],
    ["18V404000", "19V782000"],
    ["20V012000", "20V588000"],
    ["21V165000", "21V387000"],
    ["21V652000", "22V346000"],
    ["22V554000", "22V746000"],
    ["23V023000", "23V198000"],
    ["23V268000", "23V477000"],
    ["24V051000", "24V081000"],
]


def save_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError("prepared_input_changed_preserve_old_version:" + str(path))
        return
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index-digest", help="Reuse an explicitly pinned local index after preparation failure"
    )
    parser.add_argument("--output", type=Path, default=Path(".local/P1-16/prepared.json"))
    parser.add_argument("--label-root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.output.exists():
        prepared = read_json(args.output)
        for name, digest in prepared["input_sha256"].items():
            assert content_hash(read_json(Path(name))) == digest
        print(json.dumps({"reused": True, "new_requests": 0}))
        return 0
    acquisition = read_json(Path(".local/P1-16/acquisition.json"))
    if any(s["status"] != "captured" or not s["record_count"] for s in acquisition["snapshots"]):
        raise ValueError("unavailable_preselected_source_no_silent_exclusion")
    by_code = {s["source"]["selection"]["campaign_number"]: s for s in acquisition["snapshots"]}
    previous = read_json(Path("examples/retrieval-build.json"))
    request = BuildRequest.model_validate(
        previous
        | {
            "snapshot_ids": previous["snapshot_ids"]
            + [s["snapshot_id"] for s in acquisition["snapshots"]],
        }
    )
    rules = {
        "entity": read_json(Path("examples/retrieval-entities.json")),
        "scope": load_scope_rules().model_dump(mode="json"),
        "pdf": read_json(Path("examples/retrieval-pdf-scope.json")),
        "chunk": load_chunk_config("structure").model_dump(mode="json"),
        "structure": load_structure_rules().model_dump(mode="json"),
    }
    encoder, reranker = LocalModel("encoder"), LocalModel("reranker")
    if args.index_digest:
        index_root = Path(".local/P1-16/index")
        index = checked_index(index_root, args.index_digest)
        if (
            index["corpus"]["recipe"]["request"] != request.model_dump(mode="json")
            or index["encoder"] != encoder.identity
        ):
            raise ValueError("cached_preparation_index_differs_from_inputs")
        corpus, digest = index["corpus"], args.index_digest
    else:
        corpus = build_corpus(
            Path(".local/source-snapshots"),
            Path(".local/parsed-sources"),
            Path(".local/chunk-sets"),
            request,
            EntityRules.model_validate(rules["entity"]),
            load_scope_rules(),
            rules["pdf"],
        )
        index = build_index(corpus, encoder, RetrievalConfig())
        index_root = Path(".local/P1-16/index")
        digest = ArtifactStore(index_root).put_json(index)["sha256"]
        index = checked_index(index_root, digest)
    bundle = BundleStore(Path(".local/releases")).create(
        index_root,
        digest,
        raw_root=Path(".local/source-snapshots"),
        parsed_root=Path(".local/parsed-sources"),
        chunk_root=Path(".local/chunk-sets"),
        model_root=Path(".local/retrieval/models"),
        encoder_identity=encoder.identity,
        reranker_identity=reranker.identity,
        rules=rules,
    )
    base = BundleStore(Path(".local/releases")).directory(
        BundleStore(Path(".local/releases")).manifests.read_json(bundle["manifest_sha256"])
    )
    cases, refs = [], {}
    sources = {}
    for group_index, pair in enumerate(PAIRS, 1):
        cluster = f"EVAL-CLUSTER-{group_index:02}"
        for code in pair:
            snapshot = by_code[code]
            row = json.loads((base / "raw/objects" / snapshot["raw"]["sha256"]).read_bytes())[
                "results"
            ][0]
            scope = {
                "make": row["Make"],
                "model": row["Model"],
                "model_year": int(row["ModelYear"]),
                "campaign_number": code,
            }
            source_id = snapshot["source"]["source_id"]
            docs = [
                d
                for d in corpus["documents"]
                if d["metadata"]["source_id"] == source_id and d["source"]["ordinal"] == 0
            ]
            # Documents are selected by original row ordinal, never by model output/relevance score.
            if not docs:
                raise ValueError("selected_reference_row_missing")
            parsed = ArtifactStore(base / "parsed").read_json(docs[0]["parse_artifact_sha256"])
            slots, required = [], []
            for field in ("Summary", "Consequence", "Remedy"):
                unit = next(
                    u for u in parsed["units"] if (u["field_pointer"] or "").endswith("/" + field)
                )
                slots.append(
                    {
                        "slot_id": field.lower(),
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
                required.append(
                    {
                        "reference_claim_id": "R-" + field.upper(),
                        "statement": unit["text"],
                        "evidence_sets": [[field.lower()]],
                        "forbidden_inferences": [
                            "individual_applicability",
                            "causality",
                            "incidence_rate",
                            "all_vehicles_safe",
                        ],
                    }
                )
            index_number = len(cases) // 3
            category = [
                "direct",
                "cross_source",
                "counterevidence",
                "wrong_relation",
                "insufficient",
            ][index_number % 5]
            mate = by_code[next(c for c in pair if c != code)]
            allowed = [source_id]
            query = code + " summary consequence remedy conditions"
            request_body = {
                "task_id": "TEST-" + code + "-FACTS",
                "retrieval_query": query,
                "intent": "record_lookup",
                "question": "依据允许的冻结官方记录，说明缺陷、后果与补救，保留车型及适用条件；不得确认个车适用或推算故障率。",
            }
            if category in {"cross_source", "wrong_relation"}:
                allowed.append(mate["source"]["source_id"])
                request_body |= {
                    "intent": "cross_source" if category == "cross_source" else "relationship",
                    "question": "对照这两组允许记录，说明当前车型范围内的缺陷、后果与补救，并区分范围排除与未知；相似部件能否证明同一缺陷或个车适用？",
                }
                if category == "wrong_relation":
                    request_body["pairs"] = [
                        {
                            "left": {"snapshot_id": snapshot["snapshot_id"], "ordinal": 0},
                            "right": {"snapshot_id": mate["snapshot_id"], "ordinal": 0},
                        }
                    ]
            if category == "counterevidence":
                request_body["question"] += (
                    " 假设有人声称完成召回便证明所有同品牌车辆永远安全，请核对该推断并说明证据边界。"
                )
            if category == "insufficient":
                request_body |= {
                    "intent": "complaint_statistics",
                    "components": ["SERVICE BRAKES"],
                    "question": "仅依据这些允许的召回资料，能否给出投诉总数及故障发生率？若缺投诉全集应说明不足，不能把召回条数当投诉量。",
                }
            natural = {
                "case_id": request_body["task_id"],
                "event_cluster_id": cluster,
                "split": "test",
                "category": category,
                "sources": allowed,
                "scope": scope,
                "request": request_body,
            }
            reference = ReferenceCase.model_validate(
                {
                    "case_id": natural["case_id"],
                    "split": "test",
                    "slots": [] if category == "insufficient" else slots,
                    "required_claims": [] if category == "insufficient" else required,
                    "acceptable_dispositions": ["insufficient_evidence"]
                    if category == "insufficient"
                    else ["answered"],
                }
            )
            refs[natural["case_id"]] = reference.model_dump(mode="json")
            cases.append(natural | {"reference_sha256": content_hash(refs[natural["case_id"]])})
            for suffix, cat in [
                ("CONDITIONS", "missing_conditions"),
                (
                    "TIME" if index_number % 2 else "YEAR",
                    "time_boundary" if index_number % 2 else "scope_exclusion",
                ),
            ]:
                identifier = "TEST-" + code + "-" + suffix
                body = {
                    "task_id": identifier,
                    "retrieval_query": query,
                    "intent": "record_lookup",
                    "question": "请按当前任务条件检查允许的固定来源；不能用别的年份或以后取得的版本替代。",
                }
                bounded = dict(scope)
                dispositions = ["no_matching_records"]
                if cat == "missing_conditions":
                    body |= {
                        "intent": "applicability",
                        "question": "我没有提供生产日期和车辆配置，能否确认我的车适用该召回？",
                    }
                    dispositions = ["needs_clarification"]
                elif cat == "time_boundary":
                    body["expected_time"] = {
                        "mode": "historical_as_of",
                        "event_interval": None,
                        "knowledge_cutoff": "2010-01-01T00:00:00Z",
                        "date_filter_basis": "no archived version-level publication proof before cutoff",
                    }
                    dispositions = ["unsupported_time", "needs_clarification"]
                else:
                    same_model = [
                        int(r["ModelYear"])
                        for r in json.loads(
                            (base / "raw/objects" / snapshot["raw"]["sha256"]).read_bytes()
                        )["results"]
                        if r["Make"] == row["Make"] and r["Model"] == row["Model"]
                    ]
                    bounded["model_year"] = min(same_model) - 1
                ref = {
                    "case_id": identifier,
                    "split": "test",
                    "slots": [],
                    "required_claims": [],
                    "acceptable_dispositions": dispositions,
                }
                refs[identifier] = ReferenceCase.model_validate(ref).model_dump(mode="json")
                cases.append(
                    {
                        "case_id": identifier,
                        "event_cluster_id": cluster,
                        "split": "test",
                        "category": cat,
                        "sources": [source_id],
                        "scope": bounded,
                        "request": body,
                        "reference_sha256": content_hash(refs[identifier]),
                    }
                )
            sources[code] = {
                "snapshot_id": snapshot["snapshot_id"],
                "source_id": source_id,
                "raw_sha256": snapshot["raw"]["sha256"],
                "record_count": snapshot["record_count"],
                "scope": scope,
                "url": snapshot["source"]["url"],
                "event_cluster_id": cluster,
            }
    main_ids = [c["case_id"] for c in cases]
    focus = [cases[0]["case_id"], cases[30]["case_id"]]
    sequential = []
    for suffix, question in [
        ("PREFIX", "先说明允许记录中的缺陷和风险边界，不能确认个车适用。"),
        ("FOLLOWUP", "接着核对补救与适用条件；前一次结论只作线索，必须用当前原文。"),
    ]:
        copied = json.loads(json.dumps(cases[0]))
        identifier = "SEQUENCE-" + suffix
        copied |= {"case_id": identifier, "category": "direct"}
        copied["request"] |= {"task_id": identifier, "question": question}
        ref = refs[cases[0]["case_id"]] | {"case_id": identifier}
        refs[identifier] = ref
        copied["reference_sha256"] = content_hash(ref)
        cases.append(copied)
        sequential.append(identifier)
    arms = [
        {"arm_id": method, "family": "main", "method": method, "cases": main_ids}
        for method in ["fixed", "single_agent", "parallel_tools", "multi_agent"]
    ]
    arms += [
        {
            "arm_id": "retrieval_" + method,
            "family": "retrieval",
            "method": "fixed",
            "cases": focus,
            "retrieval": method,
        }
        for method in ["bm25", "dense", "hybrid", "hybrid_rerank"]
    ]
    arms += [
        {
            "arm_id": "context_" + mode,
            "family": "context",
            "method": "single_agent",
            "cases": focus,
            "context": mode,
        }
        for mode in ["full_history", "recent_window", "structured"]
    ]
    for family, targets in [("memory_frozen", focus), ("memory_sequential", sequential)]:
        arms += [
            {
                "arm_id": family + "_" + mode,
                "family": family,
                "method": "single_agent",
                "cases": targets,
                "memory": mode,
            }
            for mode in ["none", "semantic", "semantic_episode", "all"]
        ]
    prefix = {
        "schema_version": "evaluation-prefix/0.1",
        "history_policy": "Only earlier completed prefix tasks; no target answers or reference labels; source scope inspection only; reset every frozen cell and every sequential arm/repetition.",
        "declarations": [
            {
                "kind": "preference",
                "scope": "project",
                "semantic_key": "response.format",
                "value": "SYNTHETIC evaluation preference: use Chinese and list unknown conditions.",
            }
        ],
        "origin_scopes": [
            {
                "make": "TOYOTA",
                "model": "PRIUS",
                "model_year": 2010,
                "campaign_number": "10V039000",
            },
            {"make": "FORD", "model": "F-250", "model_year": 2017, "campaign_number": "17V331000"},
        ],
        "generation": "scripted_insufficient_evidence_scope_inspection_not_target_answer",
        "setup_cost_included": True,
    }
    seen = sorted(
        {
            json.loads(p.read_bytes())["source"]["selection"]["campaign_number"]
            for p in Path(".local/source-snapshots/snapshots").glob("*.json")
            if "campaign_number" in json.loads(p.read_bytes())["source"]["selection"]
            and json.loads(p.read_bytes())["source"]["usage"] != "evaluation_only"
        }
    )
    protocol = Protocol.model_validate(
        {
            "dataset_release_id": bundle["release_id"],
            "manifest_sha256": bundle["manifest_sha256"],
            "cases": cases,
            "arms": arms,
            "profile": {
                "retrieval_method": "hybrid",
                "retrieve_k": 6,
                "expand_k": 2,
                "max_tool_calls": 10,
                "max_model_calls": 8,
                "prompt_characters": 32000,
                "total_prompt_characters": 96000,
                "generation_tokens": 2000,
                "total_generation_tokens": 4000,
                "wall_seconds": 900,
                "context_mode": "structured",
                "multi_agent_enabled": True,
                "branch_timeout_seconds": 120.0,
            },
            "previously_seen_campaigns": seen,
            "cluster_campaigns": {f"EVAL-CLUSTER-{i:02}": pair for i, pair in enumerate(PAIRS, 1)},
            "frozen_prefix_sha256": content_hash(prefix),
            "quality_targets": {
                "hard_constraints": "all_declared_challenges_pass",
                "independent_review_required_for_formal_metrics": True,
                "benefit_claim_requires_paired_human_metrics_and_full_costs": True,
                "no_tuning_after_test": True,
                "repetitions_are_not_independent_events": True,
            },
        }
    )
    paths = {
        args.label_root / "protocol.json": protocol.model_dump(mode="json"),
        args.label_root / "prefix.json": prefix,
        args.label_root / "sources.json": {
            "sources": sources,
            "clustering_basis": "Conservative paired-source units plus known related Mazda inflator campaigns. These are experimental units, not proof of universal event independence.",
        },
    }
    paths |= {
        args.label_root / "references" / (identifier + ".json"): ref
        for identifier, ref in refs.items()
    }
    for path, value in paths.items():
        save_once(path, value)
    result = {
        "release": bundle,
        "sources": len(sources),
        "tasks": len(cases),
        "main_tasks": len(main_ids),
        "event_clusters": len(PAIRS),
        "input_sha256": {p.as_posix(): content_hash(v) for p, v in paths.items()},
        "new_source_requests": 0,
        "new_model_requests": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "input_sha256"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

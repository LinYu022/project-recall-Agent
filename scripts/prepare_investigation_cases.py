"""Freeze implementation-agent development references BEFORE generation. No new sources."""

import json
from pathlib import Path

from investigation.contracts import InvestigationRequest, ReferenceCase
from parsing.parser import text_hash
from releases.bundle import BundleStore
from retrieval.index import checked_index
from runtime.artifacts import ArtifactStore

ROOT = Path("evals/investigation-v0.1")


def main():
    candidates = json.loads(Path("examples/release-candidates.json").read_bytes())
    store = BundleStore(Path(candidates["root"]))
    manifest = store.manifests.read_json(candidates["candidates"]["expanded"]["manifest_sha256"])
    base = store.directory(manifest)
    corpus = checked_index(base / "index", manifest["index_artifact_sha256"])["corpus"]
    documents = corpus["documents"]

    def slot(source_id, name, *, field=None, span=None):
        doc = next(
            d
            for d in documents
            if d["metadata"]["source_id"] == source_id and (field is None or d["field"] == field)
        )
        parsed = ArtifactStore(base / "parsed").read_json(doc["parse_artifact_sha256"])
        unit = next(
            u
            for u in parsed["units"]
            if field is None or (u["field_pointer"] or "").endswith("/" + field)
        )
        start, end = span if span else (0, len(unit["text"]))
        return {
            "slot_id": name,
            "kind": "text",
            "alternatives": [
                {
                    "source_version_id": parsed["source"]["source_version_id"],
                    "unit_id": unit["unit_id"],
                    "start": start,
                    "end": end,
                    "quote_sha256": text_hash(unit["text"][start:end]),
                }
            ],
        }

    pdf = "NHTSA-INV-PE10006-CLOSE"
    prius = ["NHTSA-API-CAMPAIGN-10V039000", pdf]
    doors = ["NHTSA-API-CAMPAIGN-17V331000", "NHTSA-API-CAMPAIGN-17V652000"]
    exclusion = ["NHTSA-API-CAMPAIGN-14V284000", "NHTSA-API-CAMPAIGN-20V332000"]
    configs = [
        {
            "case_id": "DEV-PRIUS-JOINT",
            "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            "sources": prius,
            "intent": "cross_source",
            "retrieval_query": "ABS braking rough road investigation closed monitor software recall",
            "question": "基于当前允许冻结资料，说明2010 Prius的ABS相关制动问题、结案依据和后续监测；结案能否支持所有混动车均已安全？",
            "slots": [
                slot(pdf, "vehicle_scope", span=(578, 994)),
                slot(pdf, "action", span=(2167, 2234)),
                slot(pdf, "summary", span=(2836, 3446)),
                slot(prius[0], "recall_summary", field="Summary"),
            ],
            "claims": [
                {
                    "reference_claim_id": "R-SCOPE-CONDITION",
                    "statement": "2010 Prius范围内特定ABS制动条件及软件召回措施，不可泛化到所有混动车。",
                    "evidence_sets": [
                        ["vehicle_scope", "summary"],
                        ["vehicle_scope", "recall_summary"],
                    ],
                    "forbidden_inferences": ["all_hybrids_safe", "individual_vehicle_confirmed"],
                },
                {
                    "reference_claim_id": "R-CLOSE-MONITOR",
                    "statement": "基于Toyota措施结案且继续监测，不代表缺陷不存在或永远安全。",
                    "evidence_sets": [["vehicle_scope", "summary", "action"]],
                    "forbidden_inferences": ["closure_proves_no_defect"],
                },
            ],
        },
        {
            "case_id": "DEV-COUNT-86",
            "scope": {"make": "FORD", "model": "F-150", "model_year": 2016},
            "sources": ["NHTSA-COMPLAINTS-F150-2016"],
            "intent": "complaint_statistics",
            "retrieval_query": "POWER TRAIN OR SERVICE BRAKES distinct ODI count",
            "question": "当前允许的冻结2016 F-150投诉资料中，POWER TRAIN或SERVICE BRAKES去重投诉数是多少？能否据此断言故障发生率或因果？",
            "components": ["POWER TRAIN", "SERVICE BRAKES"],
            "slots": [
                {
                    "slot_id": "count86",
                    "kind": "count",
                    "expected_fields": {"status": "counted", "distinct_complaints": 86},
                }
            ],
            "claims": [
                {
                    "reference_claim_id": "R-COUNT",
                    "statement": "当前允许冻结投诉资料按ODI去重为86，不是故障发生率或因果证明。",
                    "evidence_sets": [["count86"]],
                    "forbidden_inferences": ["causality", "incidence_rate"],
                }
            ],
        },
        {
            "case_id": "DEV-DOOR-PAIR",
            "scope": {"make": "FORD", "model": "F-150", "model_year": 2016},
            "sources": doors,
            "intent": "relationship",
            "retrieval_query": "door handle cover side impact cable water freeze latch",
            "question": "17V331和17V652在2016 F-150车门部件上是否描述同一缺陷？请给出各自的缺陷事实及关系边界。",
            "pairs": [
                {
                    "left": {"snapshot_id": "f7c828b88c7c43c2a4785895d30db3df", "ordinal": 2},
                    "right": {"snapshot_id": "c0aef8c97d7d488d8eb72eb3355f866a", "ordinal": 1},
                }
            ],
            "slots": [
                slot(doors[0], "cover", field="Summary"),
                slot(doors[1], "cable", field="Summary"),
                {
                    "slot_id": "relation",
                    "kind": "relationship",
                    "expected_fields": {"status": "assessed"},
                },
            ],
            "claims": [
                {
                    "reference_claim_id": "R-COVER",
                    "statement": "17V331描述附件门把盖及侧碰问题。",
                    "evidence_sets": [["cover"]],
                },
                {
                    "reference_claim_id": "R-CABLE",
                    "statement": "17V652描述拉索弯折或进水结冰等门锁问题。",
                    "evidence_sets": [["cable"]],
                },
                {
                    "reference_claim_id": "R-DIFFERENT",
                    "statement": "同车型年款和部件不足以断言同一缺陷。",
                    "evidence_sets": [["cover", "cable", "relation"]],
                    "forbidden_inferences": ["same_defect_from_same_component"],
                },
            ],
        },
        {
            "case_id": "DEV-SCOPE-EXCLUSION",
            "scope": {"make": "FORD", "model": "F-150", "model_year": 2016},
            "sources": exclusion,
            "intent": "relationship",
            "retrieval_query": "14V284 20V332 model year scope brake master cylinder",
            "question": "14V284中2008 Escape的这条车型记录，能否用于确认2016 F-150适用？与20V332比较，区分已知排除和条件未知。",
            "pairs": [
                {
                    "left": {"snapshot_id": "7e2a7f7b671a41b29b70ca4287c5d7fe", "ordinal": 0},
                    "right": {"snapshot_id": "a383e549c8274613a765266427173a1d", "ordinal": 0},
                }
            ],
            "slots": [
                {
                    "slot_id": "exclusion",
                    "kind": "relationship",
                    "expected_fields": {
                        "status": "assessed",
                        "endpoint_scope_channels": ["excluded", "matched"],
                    },
                }
            ],
            "claims": [
                {
                    "reference_claim_id": "R-EXCLUDE",
                    "statement": "已知车型年款不匹配足以排除此Escape行作为2016 F-150适用证据；不证明任何个车安全。",
                    "evidence_sets": [["exclusion"]],
                    "forbidden_inferences": ["no_defects", "individual_vehicle_confirmed"],
                }
            ],
        },
    ]
    # Guards use these same previously seen development sources, no generation call.
    for identifier, intent, scope, extra, disposition in [
        (
            "DEV-ZERO",
            "record_lookup",
            {"make": "TOYOTA", "model": "PRIUS", "model_year": 2025},
            {},
            "no_matching_records",
        ),
        (
            "DEV-MISSING",
            "applicability",
            {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            {},
            "needs_clarification",
        ),
        (
            "DEV-TIME",
            "record_lookup",
            {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            {
                "expected_time": {
                    "mode": "historical_as_of",
                    "knowledge_cutoff": "2014-01-01T00:00:00Z",
                    "event_interval": None,
                    "date_filter_basis": "explicit request, no pre-cutoff observed receipts",
                }
            },
            "unsupported_time",
        ),
        (
            "DEV-INSUFFICIENT",
            "complaint_statistics",
            {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
            {},
            "insufficient_evidence",
        ),
    ]:
        configs.append(
            {
                "case_id": identifier,
                "intent": intent,
                "scope": scope,
                "sources": prius,
                "question": "检查当前冻结资料和必要业务条件后回答；不得扩展允许来源。",
                "retrieval_query": "recall scope",
                "extra": extra,
                "slots": [],
                "claims": [],
                "dispositions": [disposition],
            }
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    cases = []
    for config in configs:
        identifier = config["case_id"]
        reference = ReferenceCase.model_validate(
            {
                "case_id": identifier,
                "split": "development",
                "slots": config["slots"],
                "required_claims": config["claims"],
                "acceptable_dispositions": config.get("dispositions", ["answered"]),
            }
        )
        request = InvestigationRequest.model_validate(
            {
                "task_id": identifier,
                "intent": config["intent"],
                "question": config["question"],
                "retrieval_query": config["retrieval_query"],
                "expected_release_id": manifest["release_id"],
                "components": config.get("components", []),
                "pairs": config.get("pairs", []),
                **config.get("extra", {}),
            }
        )
        (ROOT / (identifier + ".json")).write_text(
            reference.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        cases.append(
            {
                "case_id": identifier,
                "scope": config["scope"],
                "sources": config["sources"],
                "request": request.model_dump(mode="json"),
            }
        )
    plan = {
        "schema_version": "fixed-baseline-cases/0.1",
        "label_author": "implementation_agent",
        "independent_human_review": False,
        "usage": "previously_seen_development_only_no_unseen_test_claim",
        "config": {
            "version": "fixed-investigation/0.1",
            "retrieval_method": "hybrid",
            "retrieve_k": 6,
            "expand_k": 2,
            "max_tool_calls": 6,
            "max_model_calls": 1,
            "prompt_characters": 24000,
            "generation_tokens": 2000,
        },
        "cases": cases,
    }
    (ROOT / "cases.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"references_frozen": len(cases), "model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

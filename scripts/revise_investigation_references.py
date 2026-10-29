"""Explicit development label correction; retain v0.1 and rescore every saved round."""

import json
from copy import deepcopy
from pathlib import Path

from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from investigation.scoring import score


def main():
    old, new = Path("evals/investigation-v0.1"), Path("evals/investigation-v0.2")
    new.mkdir(parents=True, exist_ok=True)
    changes = []
    for path in sorted(old.glob("DEV-*.json")):
        original = ReferenceCase.model_validate_json(path.read_bytes())
        revised = deepcopy(original)
        if original.case_id == "DEV-COUNT-86":
            revised.required_claims[0].statement = "当前允许冻结投诉资料按ODI去重为86。"
            revised.required_claims.extend(
                [
                    {
                        "reference_claim_id": "R-NO-RATE",
                        "statement": "该投诉计数不能证明故障发生率。",
                        "evidence_sets": [["count86"]],
                    },
                    {
                        "reference_claim_id": "R-NO-CAUSE",
                        "statement": "该投诉计数不能证明因果关系。",
                        "evidence_sets": [["count86"]],
                    },
                ]
            )
        elif original.case_id == "DEV-PRIUS-JOINT":
            revised.required_claims[1].statement = "ODI基于Toyota的召回措施将该2010 Prius调查结案。"
            revised.required_claims.append(
                {
                    "reference_claim_id": "R-MONITOR",
                    "statement": "ODI仍将继续监测并视情况采取行动。",
                    "evidence_sets": [["vehicle_scope", "summary"]],
                    "forbidden_inferences": ["closure_proves_no_defect", "all_hybrids_safe"],
                }
            )
        # Revalidate dictionaries added above; no model-derived citation or answer is inserted.
        revised = ReferenceCase.model_validate(revised.model_dump(mode="json", warnings=False))
        (new / path.name).write_text(revised.model_dump_json(indent=2) + "\n", encoding="utf-8")
        if revised != original:
            changes.append(
                {
                    "case_id": original.case_id,
                    "old_sha256": content_hash(original.model_dump(mode="json")),
                    "new_sha256": content_hash(revised.model_dump(mode="json")),
                    "reason": "split independently assessable closure/monitoring or count/rate/causation conclusions; evidence slots unchanged",
                }
            )
    for root in [
        Path(".local/P1-07"),
        *[Path(".local/P1-07") / name for name in ("round1", "round2", "round3")],
    ]:
        for report in (root / "reports").glob("DEV-*.json"):
            labels = new / report.name
            if labels.exists():
                result = score(
                    json.loads(report.read_bytes()),
                    ReferenceCase.model_validate_json(labels.read_bytes()),
                )
                target = root / "scores-v0.2" / report.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
    (new / "revision.json").write_text(
        json.dumps(
            {
                "from": "v0.1",
                "to": "v0.2",
                "author": "implementation_agent",
                "independent_human_review": False,
                "timing": "development correction after inspected outputs; never an unseen test",
                "policy": "retain old results, apply to all saved rounds; no evidence slot or admissible disposition change",
                "changes": changes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"revised_cases": len(changes), "human_reviews": 0, "model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Create explicitly attributed implementation-agent draft reviews, never independent human labels.

Judgments below were made by reading these fixed development outputs and their evidence.
This is a reproducible annotation fixture, not an automatic semantic judge for arbitrary outputs.
"""

import json
from pathlib import Path

from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from investigation.scoring import SemanticReview, score


def main():
    root = Path(".local/P1-07")
    references = Path("evals/investigation-v0.2")
    records = []
    for path in sorted((root / "reports").glob("*.json")):
        report = json.loads(path.read_bytes())
        reference = ReferenceCase.model_validate_json((references / path.name).read_bytes())
        # Do not fabricate judgments for changed model answers. Pin reviewed statement hashes.
        fixture_path = Path("evals/investigation-v0.2/draft-review-fixtures.json")
        fixtures = json.loads(fixture_path.read_bytes()) if fixture_path.exists() else {}
        fixture = fixtures.get(path.stem)
        if not fixture:
            records.append(
                {"case_id": path.stem, "status": "pending", "reason": "no_explicit_review_fixture"}
            )
            continue
        if content_hash(report) != fixture["reviewed_report_content_sha256"]:
            records.append(
                {
                    "case_id": path.stem,
                    "status": "pending",
                    "reason": "output_changed_requires_new_review",
                }
            )
            continue
        review = SemanticReview.model_validate(
            {
                "report_sha256": content_hash(report),
                "reference_sha256": content_hash(reference.model_dump(mode="json")),
                "reviewer_kind": "implementation_agent",
                "reviewer_id": "codex-implementation-review-P1-07",
                **fixture["review"],
            }
        )
        output = root / "draft-reviews" / path.name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
        scored = score(report, reference, review)
        (root / "scores" / path.name).write_text(
            json.dumps(scored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        records.append(
            {
                "case_id": path.stem,
                "reviewer_kind": "implementation_agent",
                "review_content_sha256": content_hash(review.model_dump(mode="json")),
                "formal_semantic_metrics": None,
                "provisional_semantic_metrics": scored["provisional_semantic_metrics"],
            }
        )
    output = root / "draft-review-summary.json"
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "draft_reviews": sum(
                    r.get("reviewer_kind") == "implementation_agent" for r in records
                ),
                "human_reviews": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

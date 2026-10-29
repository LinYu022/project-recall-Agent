"""Run a bound investigation, export a blind review, or score a saved report."""

import argparse
import json
from pathlib import Path

from investigation.contracts import InvestigationRequest, ReferenceCase
from investigation.review import export_blind, import_blind_review
from investigation.scoring import SemanticReview, score


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "blind", "score", "import-review"])
    parser.add_argument("input", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--private-map", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--method", choices=["fixed", "single_agent", "parallel_tools"], default="fixed"
    )
    parser.add_argument(
        "--context-mode", choices=["full_history", "structured"], default="structured"
    )
    args = parser.parse_args()
    value = json.loads(args.input.read_bytes())
    if args.action == "run":
        import os

        import sqlalchemy as sa

        from database.config import database_url
        from investigation.flow import FixedInvestigator
        from models.chat_api import ChatJSONModel
        from releases.bundle import BundleStore
        from releases.catalog import Catalog
        from releases.service import PublishedService

        engine = sa.create_engine(database_url(), hide_parameters=True)
        try:
            service = PublishedService(
                Catalog(
                    engine,
                    BundleStore(Path(os.environ.get("RECALL_RELEASE_ROOT", ".local/releases"))),
                ),
                Path(".local/releases/traces"),
            )
            from investigation.agent import SingleAgent

            runner = (
                SingleAgent
                if args.method in {"single_agent", "parallel_tools"}
                else FixedInvestigator
            )
            from investigation.contracts import FixedConfig

            extra: dict = {"parallel_tools": True} if args.method == "parallel_tools" else {}
            result = runner(
                service,
                ChatJSONModel(),
                Path(".local/investigations"),
                FixedConfig(max_model_calls=6, context_mode=args.context_mode)
                if args.method in {"single_agent", "parallel_tools"}
                else None,
                **extra,
            ).run(InvestigationRequest.model_validate(value))
        finally:
            engine.dispose()
    elif args.action == "blind":
        print(export_blind(value, args.output))
        return 0
    elif args.action == "import-review":
        from ingestion.records import content_hash

        if not args.reference or not args.private_map:
            parser.error("import-review requires --reference and --private-map")
        reference = ReferenceCase.model_validate_json(args.reference.read_bytes())
        result = import_blind_review(
            value,
            json.loads(args.private_map.read_bytes()),
            content_hash(reference.model_dump(mode="json")),
        ).model_dump(mode="json")
    else:
        if not args.reference:
            parser.error("score requires --reference")
        reference = ReferenceCase.model_validate_json(args.reference.read_bytes())
        review = (
            SemanticReview.model_validate_json(args.review.read_bytes()) if args.review else None
        )
        result = score(value, reference, review)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": args.output.as_posix(), "status": result.get("execution_status", "saved")}
        )
    )
    return 1 if result.get("execution_status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

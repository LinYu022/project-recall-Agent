"""Local prepare/import/analyze for an explicitly post hoc review revision."""

import argparse
import json
from pathlib import Path

from reviewing.corpus import analyze, import_review, prepare, read, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("prepare")
    seal.add_argument("--experiment", type=Path, required=True)
    seal.add_argument("--output", type=Path, default=Path(".local/P1-16/role-aware-reviews"))
    seal.add_argument("--trusted-human-source", type=Path, action="append")
    for name in ["import", "import-existing", "analyze"]:
        command = sub.add_parser(name)
        command.add_argument("--assessment", type=Path, required=True)
        if name == "import":
            command.add_argument("--review", type=Path, required=True)
        else:
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        pointer = prepare(args.experiment, args.output, args.trusted_human_source)
        print(json.dumps(pointer))
    elif args.command == "import":
        result = import_review(read(args.assessment), read(args.review))
        print(json.dumps({"status": result["status"], "reviewer_kind": result["reviewer_kind"]}))
    elif args.command == "import-existing":
        from reviewing.legacy import existing_human_review

        value = existing_human_review(read(args.assessment))
        write_new(args.output, value)
        result = import_review(read(args.assessment), value)
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "source": "existing_actual_human_replies_only",
                    "output": args.output.as_posix(),
                }
            )
        )
    else:
        result = analyze(read(args.assessment))
        write_new(args.output, result)
        print(
            json.dumps(
                {
                    "output": args.output.as_posix(),
                    "human_task_judgments": result["human_task_judgments"],
                    "frozen_denominator": result["frozen_denominator"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare or execute a separately authorized quality-review batch."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--root", type=Path, required=True)
    live = sub.add_parser("calibrate")
    live.add_argument("--plan", type=Path, required=True)
    live.add_argument("--approval", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--repair-invalid", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        from quality.preparation import prepare as build

        pointer, plan, lengths = build(args.root)
        print(
            json.dumps(
                {
                    "pointer": pointer,
                    "jobs": len(plan["jobs"]),
                    "calibration_jobs": len(plan["calibration_jobs"]),
                    "maximum_input_characters": max(lengths.values()),
                }
            )
        )
    else:
        from quality.runner import run
        from reviewing.corpus import read

        run(read(args.plan), args.approval, args.output, repair_invalid=args.repair_invalid)


if __name__ == "__main__":
    main()

import argparse
import json
from pathlib import Path

from baselining.preparation import prepare
from reviewing.corpus import read


def main():
    parser = argparse.ArgumentParser(
        description="Separate development pilot; preview spends no calls"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    for name in ("preview", "live"):
        command = sub.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--bin-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare()))
    else:
        from baselining.runner import run

        result = run(
            read(args.plan), args.bin_dir, mode="live" if args.command == "live" else "preview"
        )
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

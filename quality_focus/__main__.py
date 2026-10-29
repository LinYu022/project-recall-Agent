import argparse
from pathlib import Path

from quality_focus.workflow import prepare, run
from reviewing.corpus import read


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    r = sub.add_parser("run")
    r.add_argument("--plan", type=Path, required=True)
    r.add_argument("--approval", type=Path, required=True)
    r.add_argument("--root", type=Path, required=True)
    r.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        pointer, _ = prepare(args.root)
        print(pointer)
    else:
        run(read(args.plan), args.approval, args.root, repair=args.repair)


if __name__ == "__main__":
    main()

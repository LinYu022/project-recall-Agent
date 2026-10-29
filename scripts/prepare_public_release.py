"""Create a reviewable, privacy-filtered source snapshot; does not upload or create a tag."""

import argparse
from pathlib import Path

from publication.export import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.output, Path.cwd())
    print(
        {"public_files": len(result["files"]), "output": args.output.as_posix(), "uploaded": False}
    )


if __name__ == "__main__":
    main()

"""Download only pinned, hash-checked public assets; inference never downloads files."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("examples/retrieval-models.json"))
    parser.add_argument("--root", type=Path, default=Path(".local/retrieval/models"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_bytes())
    for spec in manifest["models"]:
        base = args.root / spec["role"] / spec["revision"]
        for asset in spec["files"]:
            relative = Path(asset["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("model path must stay inside its pinned directory")
            target = base / relative
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".download")
                url = f"https://huggingface.co/{spec['repository']}/resolve/{spec['revision']}/{asset['path']}"
                with urlopen(url, timeout=90) as response, temporary.open("wb") as stream:
                    while block := response.read(131072):
                        stream.write(block)
                body = temporary.read_bytes()
                if (
                    len(body) != asset["byte_size"]
                    or hashlib.sha256(body).hexdigest() != asset["sha256"]
                ):
                    raise ValueError("downloaded model hash/size differs from pinned manifest")
                temporary.rename(target)
            body = target.read_bytes()
            if (
                len(body) != asset["byte_size"]
                or hashlib.sha256(body).hexdigest() != asset["sha256"]
            ):
                raise ValueError("existing model asset differs; no automatic replacement")
        print(
            json.dumps({"role": spec["role"], "revision": spec["revision"], "verified": True}),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

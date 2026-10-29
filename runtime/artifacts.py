"""Content-addressed JSON artifacts for a single local run."""

import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4


class ArtifactIntegrityError(ValueError):
    pass


def native_path(path: Path):
    value = str(path.resolve())
    # Windows file APIs require the extended absolute form for deep paths.
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return Path(value)


class ArtifactStore:
    def __init__(self, root: Path):
        self.directory = root / "artifacts"

    def put_json(self, value: dict) -> dict:
        native_path(self.directory).mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.directory / f"{digest}.json"
        staging = self.directory / ("tmp-" + uuid4().hex)
        try:
            with native_path(staging).open("xb") as stream:
                stream.write(payload)
            try:
                # Publish complete bytes atomically, without replacing an existing object.
                os.link(native_path(staging), native_path(path))
            except FileExistsError:
                if native_path(path).read_bytes() != payload:
                    raise ArtifactIntegrityError(
                        "existing artifact does not match its content hash"
                    )
        finally:
            native_path(staging).unlink(missing_ok=True)
        return {"sha256": digest, "byte_size": len(payload)}

    def read_bytes(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArtifactIntegrityError("invalid artifact digest")
        payload = native_path(self.directory / f"{digest}.json").read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ArtifactIntegrityError("artifact content hash mismatch")
        return payload

    def read_json(self, digest: str) -> dict:
        return json.loads(self.read_bytes(digest))

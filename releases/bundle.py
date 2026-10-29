"""Seal self-contained release inputs; rebuild derived corpus instead of trusting declarations."""

import hashlib
import json
import platform
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from entities.normalize import EntityRules
from entities.scopes import ScopeRules
from ingestion.records import content_hash, prepare_import
from ingestion.temporal import TemporalRequest, select_versions
from parsing.chunk_contracts import ChunkConfig, StructureRules
from parsing.chunks import checked_chunks
from releases.contracts import ReleaseManifest
from retrieval.contracts import BuildRequest
from retrieval.corpus import build_corpus
from retrieval.index import checked_index
from retrieval.vector_store import QdrantVectors
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def read_json(path: Path) -> dict:
    return json.loads(path.read_bytes())


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class BundleStore:
    def __init__(self, root: Path, *, allow_synthetic=False):
        self.root = root.resolve()
        self.allow_synthetic = allow_synthetic
        self.manifests = ArtifactStore(self.root / "manifests")

    def directory(self, manifest: dict) -> Path:
        from pydantic import TypeAdapter

        from ingestion.contracts import Digest

        key = TypeAdapter(Digest).validate_python(manifest["bundle_key"])
        target = self.root / "bundles" / key[:24]
        if target.resolve().parent != (self.root / "bundles").resolve():
            raise ValueError("bundle directory is an escaping symlink")
        return target

    def create(
        self,
        index_root: Path,
        index_digest: str,
        *,
        raw_root: Path,
        parsed_root: Path,
        chunk_root: Path,
        model_root: Path,
        encoder_identity: dict,
        reranker_identity: dict,
        rules: dict,
        retention_days=30,
    ) -> dict:
        index = checked_index(index_root, index_digest)
        if encoder_identity != index["encoder"]:
            raise ValueError("release encoder differs from index")
        synthetic = bool(encoder_identity.get("synthetic") or reranker_identity.get("synthetic"))
        if synthetic and not self.allow_synthetic:
            raise ValueError("synthetic model release is test-only")
        if type(retention_days) is not int or retention_days < 1:
            raise ValueError("retention must be at least one day")
        repository = Path(__file__).resolve().parents[1]
        environment = {
            "python": platform.python_version(),
            "uv_lock_sha256": file_sha(repository / "uv.lock"),
            "pyproject_sha256": file_sha(repository / "pyproject.toml"),
        }
        seed = {
            "index_sha256": index_digest,
            "models": [encoder_identity, reranker_identity],
            "rules": rules,
            "retention_days": retention_days,
            "environment": environment,
            "engine_version": "release-builder/0.1",
        }
        key = content_hash(seed)
        base = self.directory({"bundle_key": key})
        inventory = []

        def put(relative: str, body: bytes):
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("component must use a clean relative path")
            target = base / relative
            if not target.resolve().is_relative_to(base.resolve()):
                raise ValueError("component outside bundle")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with target.open("xb") as stream:
                    stream.write(body)
            except FileExistsError:
                if target.read_bytes() != body:
                    raise ArtifactIntegrityError(
                        "existing build component differs; preserve it for inspection"
                    )
            inventory.append(
                {"path": relative, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            )

        def put_json(relative, value):
            put(
                relative,
                (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )

        put_json("bundle.json", {"bundle_key": key, "dataset_build_id": index["dataset_build_id"]})
        put("environment/uv.lock", (repository / "uv.lock").read_bytes())
        put("environment/pyproject.toml", (repository / "pyproject.toml").read_bytes())

        put(
            f"index/artifacts/{index_digest}.json",
            ArtifactStore(index_root).read_bytes(index_digest),
        )
        request = BuildRequest.model_validate(index["corpus"]["recipe"]["request"])
        mapping = {}
        snapshots = []
        for identifier in sorted(request.snapshot_ids):
            batch = prepare_import(raw_root, identifier)
            snapshots.append(batch.snapshot.model_dump(mode="json"))
            assert batch.snapshot.raw is not None
            raw_digest = batch.snapshot.raw.sha256
            put_json(f"raw/snapshots/{identifier}.json", snapshots[-1])
            if not any(e["path"] == f"raw/objects/{raw_digest}" for e in inventory):
                put(f"raw/objects/{raw_digest}", (raw_root / "objects" / raw_digest).read_bytes())
            for row in batch.rows:
                if row.version is not None:
                    mapping[row.version["version_id"]] = row.version
        for category, root, field in [
            ("parsed", parsed_root, "parse_artifact_sha256"),
            ("chunks", chunk_root, "chunk_artifact_sha256"),
        ]:
            for digest in sorted({d[field] for d in index["corpus"]["documents"]}):
                put(f"{category}/artifacts/{digest}.json", ArtifactStore(root).read_bytes(digest))
        for name, value in rules.items():
            if name not in {"entity", "scope", "pdf", "structure", "chunk"}:
                raise ValueError("unknown rules component")
            put_json(f"rules/{name}.json", value)
        put_json(
            "mapping.json",
            {
                "mapping_version": "source-record/0.1",
                "records": sorted(mapping.values(), key=lambda v: v["version_id"]),
            },
        )
        for name in ("bm25", "vectors", "config"):
            put_json(
                f"components/{name}.json",
                {"dataset_build_id": index["dataset_build_id"], "value": index[name]},
            )
        put_json(
            "models/identities.json", {"encoder": encoder_identity, "reranker": reranker_identity}
        )
        specs = []
        if not synthetic:
            for identity in [encoder_identity, reranker_identity]:
                spec = identity["spec"]
                specs.append(spec)
                for asset in spec["files"]:
                    source = model_root / spec["role"] / spec["revision"] / asset["path"]
                    body = source.read_bytes()
                    if (
                        len(body) != asset["byte_size"]
                        or hashlib.sha256(body).hexdigest() != asset["sha256"]
                    ):
                        raise ArtifactIntegrityError("model bytes differ before sealing release")
                    put(f"models/{spec['role']}/{spec['revision']}/{asset['path']}", body)
        put_json(
            "models/manifest.json", {"schema_version": "retrieval-models/0.1", "models": specs}
        )
        qdrant = QdrantVectors(index, index_digest, base / "qdrant", create=True, compact=True)
        qdrant.close()
        component_path = f"qdrant/{index_digest[:24]}/component.json"
        inventory.append(
            {
                "path": component_path,
                "sha256": file_sha(base / component_path),
                "bytes": (base / component_path).stat().st_size,
            }
        )
        body = {
            "schema_version": "dataset-release/0.1",
            "bundle_key": key,
            "dataset_build_id": index["dataset_build_id"],
            "index_artifact_sha256": index_digest,
            "inventory": sorted(inventory, key=lambda e: e["path"]),
            "snapshot_ids": sorted(request.snapshot_ids),
            "record_hashes": {k: content_hash(v) for k, v in sorted(mapping.items())},
            "retention_days": retention_days,
            "test_only": synthetic,
            "environment": environment,
            "allowed_source_ids": sorted(
                {d["metadata"]["source_id"] for d in index["corpus"]["documents"]}
            ),
            "qdrant_integrity": "logical_vectors_and_payloads_plus_component_manifest_not_mutable_sqlite_bytes",
        }
        manifest = body | {"release_id": content_hash(body)}
        self.verify_contents(manifest)
        ref = self.manifests.put_json(manifest)
        return {
            "release_id": manifest["release_id"],
            "manifest_sha256": ref["sha256"],
            "dataset_build_id": index["dataset_build_id"],
            "status": "sealed",
        }

    def verify(self, manifest_sha256: str, release_id: str | None = None) -> dict:
        manifest = self.manifests.read_json(manifest_sha256)
        if release_id is not None and manifest["release_id"] != release_id:
            raise ArtifactIntegrityError("requested release and manifest differ")
        self.verify_contents(manifest)
        return manifest

    def verify_contents(self, manifest: dict) -> dict:
        try:
            ReleaseManifest.model_validate(manifest)
        except ValueError as error:
            raise ArtifactIntegrityError("invalid release manifest contract") from error
        body = {k: v for k, v in manifest.items() if k != "release_id"}
        if (
            manifest.get("schema_version") != "dataset-release/0.1"
            or content_hash(body) != manifest["release_id"]
        ):
            raise ArtifactIntegrityError("release manifest identity differs")
        if manifest["test_only"] and not self.allow_synthetic:
            raise ValueError("synthetic release not allowed")
        base = self.directory(manifest)
        if read_json(base / "bundle.json") != {
            "bundle_key": manifest["bundle_key"],
            "dataset_build_id": manifest["dataset_build_id"],
        }:
            raise ArtifactIntegrityError(
                "short directory collides with another full bundle identity"
            )
        paths = []
        for entry in manifest["inventory"]:
            path = (base / entry["path"]).resolve()
            if not path.is_relative_to(base.resolve()) or entry["path"] in paths:
                raise ArtifactIntegrityError("duplicate or escaping component path")
            paths.append(entry["path"])
            if path.stat().st_size != entry["bytes"] or file_sha(path) != entry["sha256"]:
                raise ArtifactIntegrityError("release component hash/size differs")
        index = checked_index(base / "index", manifest["index_artifact_sha256"])
        if index["dataset_build_id"] != manifest["dataset_build_id"]:
            raise ArtifactIntegrityError("index belongs to another build")
        for name in ("bm25", "vectors", "config"):
            if read_json(base / f"components/{name}.json") != {
                "dataset_build_id": manifest["dataset_build_id"],
                "value": index[name],
            }:
                raise ArtifactIntegrityError("derived component belongs to another build")
        rules = {
            name: read_json(base / f"rules/{name}.json")
            for name in ("entity", "scope", "pdf", "structure", "chunk")
        }
        request = BuildRequest.model_validate(index["corpus"]["recipe"]["request"])
        if sorted(request.snapshot_ids) != manifest["snapshot_ids"]:
            raise ArtifactIntegrityError("release snapshot selection differs")
        selection = select_versions(
            base / "raw", TemporalRequest(snapshot_ids=request.snapshot_ids, time=request.time)
        )
        accepted = {v["version_id"] for v in selection["selected"]}
        scratch = self.root / "verification"
        scratch.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=scratch, prefix="replay-") as directory:
            if Path(directory).resolve().parent != scratch.resolve():
                raise ValueError("verification scratch directory escaped its owned root")
            replayed = build_corpus(
                base / "raw",
                Path(directory) / "parsed",
                Path(directory) / "chunks",
                request,
                EntityRules.model_validate(rules["entity"]),
                ScopeRules.model_validate(rules["scope"]),
                rules["pdf"],
                chunk_config=ChunkConfig.model_validate(rules["chunk"]),
                structure_rules=StructureRules.model_validate(rules["structure"]),
            )
        if replayed != index["corpus"]:
            raise ArtifactIntegrityError("release corpus differs from original-byte/rule replay")
        for digest in {d["chunk_artifact_sha256"] for d in replayed["documents"]}:
            checked_chunks(base / "raw", base / "parsed", base / "chunks", digest)
        mapping = read_json(base / "mapping.json")
        actual = {}
        for identifier in request.snapshot_ids:
            for row in prepare_import(base / "raw", identifier).rows:
                if row.version is not None:
                    actual[row.version["version_id"]] = row.version
        if mapping != {
            "mapping_version": "source-record/0.1",
            "records": sorted(actual.values(), key=lambda v: v["version_id"]),
        } or manifest["record_hashes"] != {k: content_hash(v) for k, v in sorted(actual.items())}:
            raise ArtifactIntegrityError("mapping component differs from original-byte replay")
        if not accepted <= set(actual):
            raise ArtifactIntegrityError("selected record missing")
        identities = read_json(base / "models/identities.json")
        if identities["encoder"] != index["encoder"]:
            raise ArtifactIntegrityError("release model/index mismatch")
        if manifest["test_only"] != bool(
            identities["encoder"].get("synthetic") or identities["reranker"].get("synthetic")
        ):
            raise ArtifactIntegrityError(
                "release test/production profile differs from model identities"
            )
        seed = {
            "index_sha256": manifest["index_artifact_sha256"],
            "models": [identities["encoder"], identities["reranker"]],
            "rules": rules,
            "retention_days": manifest["retention_days"],
            "environment": manifest["environment"],
            "engine_version": "release-builder/0.1",
        }
        if content_hash(seed) != manifest["bundle_key"]:
            raise ArtifactIntegrityError("release bundle key differs from its build recipe")
        required = {
            "bundle.json",
            "environment/uv.lock",
            "environment/pyproject.toml",
            "mapping.json",
            "models/identities.json",
            "models/manifest.json",
            f"index/artifacts/{manifest['index_artifact_sha256']}.json",
            f"qdrant/{manifest['index_artifact_sha256'][:24]}/component.json",
        }
        required.update(f"rules/{name}.json" for name in rules)
        required.update(f"components/{name}.json" for name in ("bm25", "vectors", "config"))
        for identifier in request.snapshot_ids:
            raw = read_json(base / f"raw/snapshots/{identifier}.json")
            required.update(
                [f"raw/snapshots/{identifier}.json", f"raw/objects/{raw['raw']['sha256']}"]
            )
        for doc in index["corpus"]["documents"]:
            required.update(
                [
                    f"parsed/artifacts/{doc['parse_artifact_sha256']}.json",
                    f"chunks/artifacts/{doc['chunk_artifact_sha256']}.json",
                ]
            )
        if not manifest["test_only"]:
            expected = [identities[role]["spec"] for role in ("encoder", "reranker")]
            if read_json(base / "models/manifest.json")["models"] != expected:
                raise ArtifactIntegrityError("model manifest differs from pinned identities")
            for spec in expected:
                for asset in spec["files"]:
                    asset_path = base / f"models/{spec['role']}/{spec['revision']}/{asset['path']}"
                    if (
                        asset_path.stat().st_size != asset["byte_size"]
                        or file_sha(asset_path) != asset["sha256"]
                    ):
                        raise ArtifactIntegrityError("model asset differs from its pinned identity")
                required.update(
                    f"models/{spec['role']}/{spec['revision']}/{asset['path']}"
                    for asset in spec["files"]
                )
        if set(paths) != required:
            raise ArtifactIntegrityError(
                "release component inventory is incomplete or contains undeclared roles"
            )
        if manifest["environment"] != {
            "python": platform.python_version(),
            "uv_lock_sha256": file_sha(base / "environment/uv.lock"),
            "pyproject_sha256": file_sha(base / "environment/pyproject.toml"),
        }:
            raise ArtifactIntegrityError("release environment files or interpreter differ")
        qdrant = QdrantVectors(
            index, manifest["index_artifact_sha256"], base / "qdrant", compact=True
        )
        qdrant.close()
        return index

    def remove_retired_bundle(self, manifest: dict):
        # The catalog must first prevent new bindings and check all retention pins.
        target = self.directory(manifest).resolve()
        intended = (self.root / "bundles").resolve()
        if target.parent != intended or not target.is_relative_to(intended):
            raise ValueError("refuse deletion outside exact release bundle")
        if target.exists():
            shutil.rmtree(target)

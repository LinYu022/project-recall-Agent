"""Bundle replay and incomplete/cross-version failure tests use explicitly synthetic releases."""

from copy import deepcopy

import pytest

from releases.bundle import BundleStore
from runtime.artifacts import ArtifactIntegrityError
from tests.release_fixtures import alter_component, write_changed_manifest
from tests.release_fixtures import release_factory as release_factory


def test_sealed_manifest_replays_without_repairing_or_rewriting_inputs(release_factory):
    bundle = release_factory("A")
    store = bundle["store"]
    before = {
        e["path"]: (bundle["directory"] / e["path"]).stat().st_mtime_ns
        for e in bundle["manifest"]["inventory"]
    }
    assert store.verify(bundle["sealed"]["manifest_sha256"]) == bundle["manifest"]
    assert before == {p: (bundle["directory"] / p).stat().st_mtime_ns for p in before}


@pytest.mark.parametrize(
    "prefix",
    ["raw/objects/", "parsed/artifacts/", "chunks/artifacts/", "components/bm25", "qdrant/"],
)
def test_missing_component_is_rejected_and_never_recreated(release_factory, prefix):
    bundle = release_factory("A")
    relative = next(
        e["path"] for e in bundle["manifest"]["inventory"] if e["path"].startswith(prefix)
    )
    path = bundle["directory"] / relative
    path.unlink()
    with pytest.raises((FileNotFoundError, ArtifactIntegrityError)):
        bundle["store"].verify(bundle["sealed"]["manifest_sha256"])
    assert not path.exists()


@pytest.mark.parametrize("component", ["bm25", "vectors", "config"])
def test_rehashed_component_from_other_build_cannot_pass(release_factory, component):
    first = release_factory("A")
    second = release_factory("B")
    forged = alter_component(
        first,
        f"components/{component}.json",
        {
            "dataset_build_id": second["index"]["dataset_build_id"],
            "value": second["index"][component],
        },
    )
    with pytest.raises(ArtifactIntegrityError, match="another build"):
        first["store"].verify_contents(forged)


def test_omitting_inventory_entry_and_rehashing_manifest_is_rejected(release_factory):
    bundle = release_factory("A")
    manifest = deepcopy(bundle["manifest"])
    manifest["inventory"] = [
        e for e in manifest["inventory"] if e["path"] != "models/manifest.json"
    ]
    with pytest.raises(ArtifactIntegrityError, match="inventory"):
        bundle["store"].verify_contents(write_changed_manifest(manifest))


def test_fixed_bundle_rules_do_not_read_new_repository_defaults(release_factory, monkeypatch):
    bundle = release_factory("A")

    def refuse(*args):
        raise AssertionError("mutable defaults must not be used")

    monkeypatch.setattr("retrieval.corpus.load_chunk_config", refuse)
    monkeypatch.setattr("retrieval.corpus.load_structure_rules", refuse)
    assert bundle["store"].verify(bundle["sealed"]["manifest_sha256"])


def test_test_only_release_cannot_be_opened_by_production_store(release_factory):
    bundle = release_factory("A")
    with pytest.raises(ValueError, match="synthetic"):
        BundleStore(bundle["store"].root).verify(bundle["sealed"]["manifest_sha256"])


def test_manifest_and_bundle_path_cannot_cross_release_identity(release_factory):
    bundle = release_factory("A")
    with pytest.raises(ArtifactIntegrityError):
        bundle["store"].verify(bundle["sealed"]["manifest_sha256"], "0" * 64)
    forged = deepcopy(bundle["manifest"])
    forged["bundle_key"] = "../outside"
    with pytest.raises(ValueError):
        bundle["store"].remove_retired_bundle(forged)

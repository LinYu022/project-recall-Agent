"""Shared artifacts must never expose partially written bytes to another branch."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


def test_identical_concurrent_results_publish_one_complete_immutable_artifact(tmp_path):
    store = ArtifactStore(tmp_path)
    barrier = Barrier(8)
    value = {"kind": "SYNTHETIC concurrent result", "text": "abc" * 100000}

    def write(_):
        barrier.wait(5)
        ref = store.put_json(value)
        assert store.read_json(ref["sha256"]) == value
        return ref

    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(write, range(8)))
    assert len({r["sha256"] for r in refs}) == 1
    assert len(list(store.directory.iterdir())) == 1
    target = store.directory / (refs[0]["sha256"] + ".json")
    target.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.put_json(value)

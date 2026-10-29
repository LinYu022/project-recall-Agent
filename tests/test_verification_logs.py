"""Verification must not erase historical logs or arbitrary pytest directories."""

from pathlib import Path

import pytest

from scripts.verify_offline import prepare_pytest_root, prepare_root


def test_default_verification_runs_have_distinct_log_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first, second = prepare_root(None), prepare_root(None)
    assert first != second and first.is_dir() and second.is_dir()


def test_existing_verification_logs_are_refused_without_deletion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = prepare_root(Path(".local/old-run"))
    proof = root / "old-proof.txt"
    proof.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_root(root)
    assert proof.read_text(encoding="utf-8") == "keep"


def test_log_target_cannot_be_workspace_root_or_escape_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        prepare_root(Path.cwd())
    with pytest.raises(ValueError):
        prepare_root(tmp_path.parent / "outside-verification")


def test_pytest_temp_is_fresh_short_and_independent_of_historical_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logs = prepare_root(Path(".local/descriptive-historical-log-name"))
    proof = logs / "proof.txt"
    proof.write_text("preserve", encoding="utf-8")
    first, second = prepare_pytest_root(), prepare_pytest_root()
    assert first != second and first.parent == second.parent == Path(".local/pt")
    assert len(first.name) == len(second.name) == 12
    assert first.is_dir() and second.is_dir()
    assert proof.read_text(encoding="utf-8") == "preserve"

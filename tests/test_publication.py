"""Publication boundaries tested against isolated temporary Git repositories."""

import json
import subprocess

import pytest

from publication.export import export, markdown_links, public_text, scan_text


def staged_repo(tmp_path, files):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    files = {
        "README.md": "private development readme",
        "docs/release/README.public.md": "public readme",
        **files,
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-qm",
            "synthetic fixture",
        ],
        cwd=root,
        check=True,
    )
    return root


def test_private_human_payload_is_not_exported_or_rewritten_in_source(tmp_path):
    private = "docs/experiments/P1-16-confirmation-human-synthetic.json"
    root = staged_repo(
        tmp_path,
        {
            private: json.dumps(
                {"source_kind": "actual_human_reply", "private_note": "SYNTHETIC PRIVATE REVIEW"}
            ),
            "code.py": "x = 1\n",
        },
    )
    original = (root / private).read_bytes()
    output = root / ".local/public"
    result = export(output, root)
    assert (root / private).read_bytes() == original
    assert "SYNTHETIC PRIVATE REVIEW" not in (output / private).read_text()
    assert (
        json.loads((output / private).read_text())["publication_status"]
        == "private_audit_not_distributed"
    )
    assert result["public_tree_is_not_the_original_frozen_experiment"]
    assert not (output / ".git").exists()
    assert (output / "README.md").read_text() == "public readme"
    with pytest.raises(FileExistsError):
        export(output, root)


def test_staged_secret_environment_file_is_rejected(tmp_path):
    root = staged_repo(tmp_path, {".env": "SYNTHETIC_SECRET=not-real\n"})
    with pytest.raises(ValueError, match="environment_file"):
        export(root / ".local/public", root)


def test_secret_pattern_in_a_normal_source_file_blocks_export(tmp_path):
    marker = "-----BEGIN " + "PRIVATE KEY-----"
    root = staged_repo(tmp_path, {"accidental.txt": marker})
    with pytest.raises(ValueError, match="public_content_scan_failed"):
        export(root / ".local/public", root)


def test_export_never_targets_source_or_outside_local(tmp_path):
    root = staged_repo(tmp_path, {})
    for target in [root, root.parent / "outside", root / "publication"]:
        with pytest.raises(ValueError, match="new_directory"):
            export(target, root)


def test_json_workspace_paths_are_relative_and_conversation_identifiers_removed(tmp_path):
    payload = json.dumps(
        {"path": str(tmp_path / ".local/evidence.json"), "source_question_id": "call_" + "a" * 32}
    )
    value = public_text(payload, workspace=tmp_path)
    decoded = json.loads(value)
    assert decoded["path"] in {".local/evidence.json", ".local\\evidence.json"}
    assert decoded["source_question_id"] == "PRIVATE_REVIEW_REFERENCE"
    assert scan_text(value) == []


def test_markdown_workspace_links_are_relative_to_the_document(tmp_path):
    text = "[guide](" + tmp_path.as_posix() + "/docs/product-guide.md)"
    assert (
        markdown_links(text, "docs/experiments/report.md", tmp_path)
        == "[guide](../product-guide.md)"
    )
    local = "[private](" + tmp_path.as_posix() + "/.local/run/report.json)"
    assert "../reproducibility.md#historical-results" in markdown_links(
        local, "docs/experiments/report.md", tmp_path
    )


def test_unstaged_readme_content_does_not_enter_staged_snapshot(tmp_path):
    root = staged_repo(tmp_path, {})
    (root / "docs/release/README.public.md").write_text("UNSTAGED PRIVATE NOTE", encoding="utf-8")
    output = root / ".local/public"
    export(output, root)
    assert (output / "README.md").read_text() == "public readme"

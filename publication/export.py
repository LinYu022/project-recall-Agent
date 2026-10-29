"""Export the staged source snapshot without private audit material or Git history."""

import hashlib
import json
import posixpath
import re
import subprocess
from pathlib import PurePosixPath


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_audit(path):
    return (
        (path.startswith("docs/experiments/") and "human" in path and path.endswith(".json"))
        or (path.startswith("docs/verification/") and "human" in path)
        or path
        in {
            "evals/quality-confirmation-v0.1/approval.json",
            "evals/quality-confirmation-v0.1/release-location.json",
        }
    )


def public_text(text, *, workspace):
    roots = [str(workspace).replace("\\", "/") + "/", str(workspace).replace("/", "\\") + "\\"]
    for root in roots:
        text = text.replace(root, "")
        text = text.replace(root.replace("\\", "\\\\"), "")
    text = re.sub(r"[A-Za-z]:[/\\]+Users[/\\]+[^\s\"<>\n)]+", "<PRIVATE_LOCAL_PATH>", text)
    text = re.sub(r"(?<![\w.])/(?:Users|home)/[^\s\"<>\n)]+", "<PRIVATE_LOCAL_PATH>", text)
    text = re.sub(r"\bcall_[a-f0-9]{28,}\b", "PRIVATE_REVIEW_REFERENCE", text)
    return text


def scan_text(text):
    patterns = {
        "private_workspace_path": r"[A-Za-z]:[/\\]+Users[/\\]+|/(?:Users|home)/[A-Za-z0-9_-]+/",
        "github_token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
        "provider_key": r"\bsk-[A-Za-z0-9]{24,}\b",
        "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "conversation_reference": r"\bcall_[a-f0-9]{28,}\b",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text)]


def markdown_links(text, path, workspace):
    prefix = workspace.as_posix() + "/"

    def rewrite(match):
        target = match[1].strip("<>")
        if target.startswith(prefix):
            target = target[len(prefix) :]
            if target.startswith(".local/"):
                target = "docs/reproducibility.md#historical-results"
            relative = posixpath.relpath(target, posixpath.dirname(path) or ".")
            return "](" + ("<" + relative + ">" if " " in relative else relative) + ")"
        return match[0]

    return re.sub(r"\]\((<?[^\n)]+>?)\)", rewrite, text)


def export(destination, workspace):
    workspace = workspace.resolve()
    destination = destination.resolve()
    if destination == workspace or not destination.is_relative_to(workspace / ".local"):
        raise ValueError("export_must_be_a_new_directory_beneath_workspace_local")
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=workspace).split(
        b"\0"
    )
    selected = []
    for entry in entries:
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, blob, stage = header.decode().split()
        path = raw_path.decode("utf-8")
        parts = PurePosixPath(path).parts
        if stage != "0" or mode not in {"100644", "100755"}:
            raise ValueError("unmerged_or_linked_source_is_not_exportable")
        if (
            PurePosixPath(path).is_absolute()
            or ".." in parts
            or any(p in {".git", ".local", ".venv", "node_modules", "__pycache__"} for p in parts)
        ):
            raise ValueError("private_or_unsafe_staged_path")
        if parts[-1].startswith(".env") and parts[-1] != ".env.example":
            raise ValueError("private_environment_file_is_staged")
        selected.append((path, blob, mode))
    destination.mkdir(parents=True, exist_ok=False)
    manifest = []
    for path, blob, mode in selected:
        original = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=workspace)
        transform = []
        if private_audit(path):
            value = {
                "publication_status": "private_audit_not_distributed",
                "original_git_blob_sha256": sha(original),
                "reason": "Actual human replies, conversation identifiers, local approvals and storage pointers remain private.",
                "historical_verdict": "not_passed",
                "summary": "See docs/experiments/P1-16-confirmation-results.md and docs/reproducibility.md.",
            }
            data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            transform.append("withheld_private_audit_payload")
        else:
            data = original
            try:
                decoded = original.decode("utf-8")
            except UnicodeDecodeError:
                decoded = None
            if decoded is not None:
                if path.endswith(".md"):
                    decoded = markdown_links(decoded, path, workspace)
                public = public_text(decoded, workspace=workspace)
                if path == "README.md":
                    public = subprocess.check_output(
                        ["git", "show", ":docs/release/README.public.md"], cwd=workspace
                    ).decode("utf-8")
                    transform.append("public_readme")
                data = public.encode("utf-8")
                if public != decoded:
                    transform.append("privacy_derivative_not_original_experiment_artifact")
                failures = scan_text(public)
                if failures:
                    raise ValueError(
                        "public_content_scan_failed:" + path + ":" + ",".join(failures)
                    )
                if path.endswith(".json"):
                    json.loads(public)
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if mode == "100755":
            target.chmod(0o755)
        manifest.append(
            {
                "path": path,
                "source_git_blob_sha256": sha(original),
                "public_sha256": sha(data),
                "transformations": transform,
            }
        )
    result = {
        "schema_version": "public-source-derivative/0.1",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
        ).strip(),
        "staged_tree": subprocess.check_output(
            ["git", "write-tree"], cwd=workspace, text=True
        ).strip(),
        "no_development_git_history_included": True,
        "original_experiment_identity_preserved_in_private_workspace_only": True,
        "public_tree_is_not_the_original_frozen_experiment": True,
        "historical_quality_verdict": "not_passed",
        "files": manifest,
        "content_scan": "passed_listed_patterns_not_a_universal_secret_detector",
    }
    (destination / "PUBLICATION-MANIFEST.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result

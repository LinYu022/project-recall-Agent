"""Report-owned log pages and original evidence expansion; never arbitrary file access."""

from investigation.prompt_context import evidence_page, json_text, text_page
from parsing.citations import resolve_citation
from parsing.contracts import Citation
from runtime.artifacts import ArtifactIntegrityError


def inspect_report(
    report, store, base, *, section, alias=None, artifact_id=None, offset=0, limit=2000
):
    if section == "evidence":
        if alias not in report["aliases"]:
            raise ValueError("unknown evidence alias")
        card = next(c for c in report["cards"] if c["evidence_id"] == report["aliases"][alias])
        if card["release_id"] != report["release_id"] or not set(card["source_ids"]) <= set(
            report["allowed_source_ids"]
        ):
            raise ArtifactIntegrityError("evidence outside published report")
        origin = store.read_json(card["origin_sha256"])
        selected = origin
        for key in card["selector"]["path"]:
            selected = selected[key]
        if selected != card["payload"]:
            raise ArtifactIntegrityError("original evidence differs from origin")
        for citation in card["payload"].get("citations", []):
            resolve_citation(base / "raw", base / "parsed", Citation.model_validate(citation))
        return evidence_page(report["cards"], report["aliases"], alias, offset, limit) | {
            "original_replay": "verified_not_semantic_support",
        }
    if section == "artifact":
        registered = report.get("context_artifacts", {})
        if artifact_id not in registered:
            raise ValueError("unknown_context_artifact_handle")
        value = store.read_json(registered[artifact_id]["sha256"])
    elif section in {"observations", "planner_revisions", "tool_calls", "context_builds"}:
        value = report.get(section, [])
    else:
        raise ValueError("unknown report section")
    return {"section": section} | text_page(json_text(value), offset, limit)

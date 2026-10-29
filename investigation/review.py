"""Blinded packages omit method/model/configuration, scores, run IDs and local paths."""

import html
import json
from pathlib import Path
from uuid import uuid4

from ingestion.records import content_hash
from investigation.scoring import SemanticReview


def blind_package(report):
    identifier = "BLIND-" + uuid4().hex[:12]
    aliases = report["aliases"] or {
        f"E{i:03}": c["evidence_id"] for i, c in enumerate(report["cards"], 1)
    }
    by_id = {c["evidence_id"]: c for c in report["cards"]}
    evidence = []
    for alias, identifier_ in aliases.items():
        card = by_id[identifier_]
        evidence.append(
            {
                "alias": alias,
                "kind": card["kind"],
                "scope_channel": card["scope_channel"],
                "payload": {
                    k: v
                    for k, v in card["payload"].items()
                    if k not in {"task_id", "trace_artifact", "published_elapsed_ms"}
                },
                "source_ids": card["source_ids"],
                "required_context": [
                    a for a, i in aliases.items() if i in card["required_context_ids"]
                ],
            }
        )
    public = {
        "schema_version": "blind-investigation/0.1",
        "blind_id": identifier,
        "question": report["request"]["question"],
        "scope": report["scope"],
        "time": report["time"],
        "allowed_source_ids": report["allowed_source_ids"],
        "proposal": report["proposal"],
        "evidence": evidence,
        "execution_failed": report["execution_status"] == "failed",
    }
    private = {
        "blind_id": identifier,
        "report_content_sha256": content_hash(report),
        "run_id": report["run_id"],
        "aliases": aliases,
    }
    return public, private


def export_blind(report, directory: Path):
    public, private = blind_package(report)
    directory.mkdir(parents=True, exist_ok=True)
    stem = public["blind_id"]
    for suffix, value in [(".json", public), (".private.json", private)]:
        (directory / (stem + suffix)).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    page = (
        "<!doctype html><meta charset='utf-8'><title>证据审查</title><style>body{max-width:1100px;margin:2rem auto;font-family:system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>证据审查</h1><pre>"
        + html.escape(json.dumps(public, ensure_ascii=False, indent=2))
        + "</pre>"
    )
    (directory / (stem + ".html")).write_text(page, encoding="utf-8")
    return public["blind_id"]


def import_blind_review(blind_review: dict, private: dict, reference_sha256: str):
    if blind_review.get("blind_id") != private["blind_id"]:
        raise ValueError("blind review mapping mismatch")
    # Reviewer supplies semantic judgments, never an automatic supported label.
    value = {k: v for k, v in blind_review.items() if k != "blind_id"}
    for decision in value.get("novel_evidence_decisions", []):
        decision["evidence_ids"] = [private["aliases"][a] for a in decision.pop("evidence_aliases")]
    return SemanticReview.model_validate(
        value
        | {"report_sha256": private["report_content_sha256"], "reference_sha256": reference_sha256}
    )

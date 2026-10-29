"""Deterministic batch audit; source text and exception messages stay out of the ledger."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract
from ingestion.records import content_hash
from parsing.chunk_contracts import ChunkConfig, StructureRules
from parsing.chunks import ChunkBuildError, build_chunks, expand_chunk
from parsing.contracts import ParseConfig, ParseRequest
from parsing.parser import parse_source
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore


class AuditManifest(Contract):
    schema_version: Literal["parse-audit-request/0.1"] = "parse-audit-request/0.1"
    requests: Annotated[list[ParseRequest], Field(min_length=1, max_length=500)]
    parse_config: ParseConfig = Field(default_factory=ParseConfig)

    @model_validator(mode="after")
    def unique_requests(self):
        keys = [(r.snapshot_id, r.ordinal) for r in self.requests]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate snapshot/row requests are not allowed")
        return self


def coverage_summary(parsed: dict, chunk_set: dict) -> dict:
    """Count source positions, not joined display text; root-only text remains navigable."""
    accessible: dict[str, list[tuple[int, int]]] = {u["unit_id"]: [] for u in parsed["units"]}
    for node in chunk_set["chunks"]:
        if node["searchable"]:
            bundle = expand_chunk(chunk_set, node["chunk_id"])
            for item in [bundle["focus"], *bundle["context"]]:
                for part in item["ranges"]:
                    accessible[part["unit_id"]].append((part["start"], part["end"]))
    gaps = []
    for unit in parsed["units"]:
        cursor = 0
        for start, end in [
            *sorted(accessible[unit["unit_id"]]),
            (len(unit["text"]), len(unit["text"])),
        ]:
            if start > cursor:
                nonspace = sum(not c.isspace() for c in unit["text"][cursor:start])
                if nonspace:
                    gaps.append(
                        {
                            "unit_id": unit["unit_id"],
                            "page_number": unit["page_number"],
                            "field_pointer": unit["field_pointer"],
                            "start": cursor,
                            "end": start,
                            "nonwhitespace_characters": nonspace,
                        }
                    )
            cursor = max(cursor, end)
    return {
        "basis": "leaf_and_expanded_context_source_positions",
        "root_only_ranges": gaps,
        "root_only_nonwhitespace_characters": sum(g["nonwhitespace_characters"] for g in gaps),
        "whole_document_root_available": True,
        "semantic_completeness": "not_evaluated",
    }


def _next_action(code: str) -> str:
    if code == "no_extractable_text_needs_review_or_ocr":
        return "review_original_page_before_deciding_ocr_record_method_and_uncertainty"
    if code in {"page_limit_or_empty_document", "text_limit_exceeded"}:
        return "review_limits_and_create_new_parse_config_never_accept_truncation"
    if code == "pdf_layout_not_reviewed":
        return "review_fixed_version_layout_and_add_versioned_structure_profile"
    if code == "root_only_text":
        return "inspect_root_only_ranges_and_decide_whether_profile_needs_revision"
    if code == "layout_not_reviewed":
        return "use_only_as_unreviewed_baseline_pending_layout_review"
    if code == "source_integrity_failed":
        return "restore_verified_original_bytes_do_not_relabel_changed_bytes"
    if code.startswith("source_"):
        return "check_pinned_snapshot_row_and_local_original_files"
    if code.startswith("chunk_"):
        return "review_recipe_and_fixed_source_create_new_rules_if_needed"
    return "inspect_original_and_parser_issue_before_retrying_no_silent_repair"


def audit_sources(
    raw_root: Path,
    parsed_root: Path,
    chunk_root: Path,
    manifest: AuditManifest,
    configs: list[ChunkConfig],
    rules: StructureRules,
) -> dict:
    if sorted(c.strategy for c in configs) != ["fixed", "structure"]:
        raise ValueError("audit requires exactly one structure and one fixed recipe")
    items: list[dict] = []
    failures: list[dict] = []
    warnings: list[dict] = []

    def issue(item, code, stage, *, page=None, strategy=None, warning=False):
        record = {
            "request": item["request"],
            "source": item["source"],
            "parse_artifact_sha256": item["parse_artifact"]["sha256"]
            if item["parse_artifact"]
            else None,
            "stage": stage,
            "strategy": strategy,
            "page_number": page,
            "code": code,
            "next_action": _next_action(code),
        }
        record["issue_id"] = content_hash(record)
        (warnings if warning else failures).append(record)
        item["warning_ids" if warning else "failure_ids"].append(record["issue_id"])

    for request in manifest.requests:
        item: dict = {
            "request": request.model_dump(mode="json"),
            "source": None,
            "parse_status": "not_parsed",
            "parse_artifact": None,
            "chunk_attempts": [],
            "failure_ids": [],
            "warning_ids": [],
        }
        items.append(item)
        try:
            parsed = parse_source(raw_root, request, manifest.parse_config)
        except ArtifactIntegrityError:
            issue(item, "source_integrity_failed", "source")
            continue
        except OSError:
            issue(item, "source_io_failed", "source")
            continue
        except ValueError:
            issue(item, "source_record_rejected", "source")
            continue
        # A disk write failure aborts the invocation: it is not a bad source document.
        ref = ArtifactStore(parsed_root).put_json(parsed)
        item.update(source=parsed["source"], parse_status=parsed["status"], parse_artifact=ref)
        if parsed["status"] != "parsed":
            for problem in parsed["issues"]:
                issue(item, problem["code"], "parse", page=problem["page_number"])
            item["chunk_attempts"] = [
                {"strategy": c.strategy, "status": "skipped_incomplete_parse"} for c in configs
            ]
            continue
        for config in configs:
            attempt: dict = {"strategy": config.strategy, "status": "blocked"}
            item["chunk_attempts"].append(attempt)
            try:
                chunks = build_chunks(raw_root, parsed_root, ref["sha256"], config, rules)
            except ChunkBuildError as error:
                issue(item, error.code, "chunk", strategy=config.strategy)
                continue
            except ArtifactIntegrityError:
                issue(item, "chunk_integrity_failed", "chunk", strategy=config.strategy)
                continue
            except OSError:
                issue(item, "chunk_source_io_failed", "chunk", strategy=config.strategy)
                continue
            except ValueError:
                issue(item, "chunk_recipe_rejected", "chunk", strategy=config.strategy)
                continue
            chunk_ref = ArtifactStore(chunk_root).put_json(chunks)
            coverage = coverage_summary(parsed, chunks)
            attempt.update(
                status="built",
                chunk_artifact=chunk_ref,
                context_status=chunks["context_status"],
                coverage=coverage,
            )
            if coverage["root_only_ranges"]:
                issue(item, "root_only_text", "coverage", strategy=config.strategy, warning=True)
            if chunks["context_status"] == "layout_not_reviewed":
                issue(item, "layout_not_reviewed", "chunk", strategy=config.strategy, warning=True)
    return {
        "schema_version": "parse-audit/0.1",
        "engine_version": "parse-auditor/0.1",
        "status": "audited",
        "quality": "failures_present" if failures else "no_blocking_failures",
        "recipe": {
            "manifest": manifest.model_dump(mode="json"),
            "chunk_configs": [c.model_dump(mode="json") for c in configs],
            "structure_rules_sha256": content_hash(rules.model_dump(mode="json")),
        },
        "items": items,
        "failures": failures,
        "warnings": warnings,
        "counts": {
            "requests": len(items),
            "parsed": sum(i["parse_status"] == "parsed" for i in items),
            "chunk_sets": sum(a["status"] == "built" for i in items for a in i["chunk_attempts"]),
            "blocked_items": sum(bool(i["failure_ids"]) for i in items),
            "failures": len(failures),
            "warnings": len(warnings),
        },
        "semantic_support": "not_evaluated",
        "dataset_release": None,
        "ocr_performed": False,
        "external_model_calls": 0,
    }

"""Task-shaped read-only queries over explicit snapshot/time inputs."""

from pathlib import Path

from pydantic import model_validator

from domain.tasks import Contract
from entities.links import LinkRequest, assess_links
from entities.scopes import RecordLocator, ScopeRules, build_scope
from ingestion.records import content_hash
from ingestion.temporal import TemporalRequest, select_versions


class TemporalLinkRequest(Contract):
    temporal: TemporalRequest
    left: RecordLocator
    right: RecordLocator

    @model_validator(mode="after")
    def endpoints_in_input_set(self):
        if not {self.left.snapshot_id, self.right.snapshot_id} <= set(self.temporal.snapshot_ids):
            raise ValueError("both endpoints must belong to the pinned temporal input set")
        if self.left == self.right:
            raise ValueError("relationship query requires different endpoint locations")
        return self


def query_links(root: Path, request: TemporalLinkRequest, rules: ScopeRules) -> dict:
    selection = select_versions(root, request.temporal)
    context = {
        "schema_version": "entity-link-query/0.1",
        "dataset_release": None,
        "selection_sha256": selection["selection_sha256"],
        "input_set_sha256": selection["input_set_sha256"],
        "inputs": selection["inputs"],
        "mapping_version": selection["mapping_version"],
        "temporal_status": selection["status"],
        "time": request.temporal.time.model_dump(mode="json"),
        "analysis_label": selection["analysis_label"],
        "scope_rules_version": rules.rules_version,
        "scope_rules_sha256": rules.sha256,
        "coverage": "selected_local_receipts_only",
        "exclusion_reasons": selection["exclusion_reasons"],
    }
    # Never expose positive links for incomplete or temporally unsupported selections.
    if selection["status"] not in {"selected", "empty"}:
        return context | {
            "status": "unsupported",
            "reason": "temporal_selection_incomplete",
            "assessment": None,
        }
    eligible = set(selection["eligible_snapshot_ids"])
    selected = {entry["version_id"] for entry in selection["selected"]}
    for locator in (request.left, request.right):
        if locator.snapshot_id not in eligible:
            return context | {
                "status": "unsupported",
                "reason": "endpoint_receipt_outside_time_scope",
                "assessment": None,
            }
        scope = build_scope(root, locator, rules)
        if scope["source"]["source_version_id"] not in selected:
            return context | {
                "status": "unsupported",
                "reason": "endpoint_not_selected",
                "assessment": None,
            }
    assessment = assess_links(root, LinkRequest(left=request.left, right=request.right), rules)
    # Keep the underlying snapshot-only artifact intact. This envelope is the temporal assertion.
    result = context | {"status": "assessed", "reason": None, "assessment": assessment}
    return result | {"query_sha256": content_hash(result)}

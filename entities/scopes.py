"""Reviewed JSON facts, verified against fixed source bytes; never inferred by a model."""

import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from entities.normalize import EntityRules, project_record
from ingestion.contracts import Digest, SnapshotID
from ingestion.records import content_hash, prepare_import


class RecordLocator(Contract):
    snapshot_id: SnapshotID
    ordinal: Annotated[int, Field(strict=True, ge=0)]


class ProductionRange(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end:
            raise ValueError("production range is reversed")
        return self


class FactRule(Contract):
    source_version_id: Digest
    field: Text
    quote: Text
    facet: Literal["production_range", "configuration", "symptom", "official_reference"]
    key: Text
    # Explicit source-reviewed canonical value; a quote match alone cannot verify semantics.
    value: Text | ProductionRange
    review_note: Text
    mentioned_identifier: Text | None = None

    @model_validator(mode="after")
    def shape(self):
        if (self.facet == "production_range") != isinstance(self.value, ProductionRange):
            raise ValueError("production_range requires two inclusive dates")
        if self.facet == "production_range" and self.key != "production_range":
            raise ValueError("production range uses the production_range key")
        if self.facet == "official_reference":
            mention = self.mentioned_identifier or self.value
            if not isinstance(self.value, str) or not isinstance(mention, str):
                raise ValueError("official reference requires a text identifier")
            # The shortened form is accepted only as an explicitly reviewed recall-number format.
            canonical = (
                mention.replace("-", "") + "000"
                if re.fullmatch(r"\d{2}V-\d{3}", mention)
                else mention
            )
            if (
                canonical != self.value
                or re.search(r"(?<!\w)" + re.escape(mention) + r"(?!\w)", self.quote) is None
            ):
                raise ValueError("official reference requires the explicit identifier in the quote")
        elif self.mentioned_identifier is not None:
            raise ValueError("mentioned_identifier is only valid for official references")
        return self


class ScopeRules(Contract):
    rules_version: Text
    entity_rules: EntityRules
    configuration_domains: dict[str, list[str]]
    facts: list[FactRule]

    @model_validator(mode="after")
    def unique_facts(self):
        identities = [(f.source_version_id, f.facet, f.key) for f in self.facts]
        if len(set(identities)) != len(identities):
            raise ValueError("conflicting facts require explicit resolution in a new rules version")
        for values in self.configuration_domains.values():
            if not values or len(set(values)) != len(values):
                raise ValueError("configuration domains require unique mutually exclusive values")
        for fact in self.facts:
            if fact.facet == "configuration" and fact.value not in self.configuration_domains.get(
                fact.key, []
            ):
                raise ValueError("configuration values require an explicit exclusive domain")
        return self

    @property
    def sha256(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def load_scope_rules() -> ScopeRules:
    path = Path(__file__).resolve().parents[1] / "examples/scope-rules.json"
    return ScopeRules.model_validate_json(path.read_bytes())


def build_scope(root: Path, locator: RecordLocator, rules: ScopeRules) -> dict:
    """Only an exact source-version rule can add conditions to an entity projection."""
    batch = prepare_import(root, locator.snapshot_id)
    if locator.ordinal >= len(batch.rows):
        raise ValueError("source ordinal is outside the verified snapshot")
    row = batch.rows[locator.ordinal]
    version = row.version
    if version is None or version["source_kind"] not in {"recall_api", "complaint_api"}:
        raise ValueError("scope facts require an accepted JSON source row")
    snapshot = batch.snapshot
    assert snapshot.raw is not None
    base = {
        "snapshot_id": locator.snapshot_id,
        "raw_sha256": snapshot.raw.sha256,
        "source_version_id": version["version_id"],
        "source_content_sha256": version["content_sha256"],
        "source_url": snapshot.source.url,
        "row_pointer": row.pointer,
    }

    def evidence(field: str, quote: str | None = None) -> dict:
        raw = version["payload"]
        for token in field.split("/"):
            if isinstance(raw, dict):
                raw = raw.get(token)
            elif isinstance(raw, list) and token.isdecimal() and int(token) < len(raw):
                raw = raw[int(token)]
            else:
                raise ValueError("evidence field path cannot be resolved")
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            raise ValueError("evidence field is missing or is not text/integer")
        text = str(raw)
        excerpt = text if quote is None else quote
        if not excerpt or text.count(excerpt) != 1:
            raise ValueError("reviewed quote must occur exactly once in its fixed source field")
        start = text.index(excerpt)
        return base | {
            "field": field,
            "field_pointer": f"{row.pointer}/{field}",
            "field_sha256": content_hash(raw),
            "quote": excerpt,
            "start": start,
            "end": start + len(excerpt),
        }

    projection = project_record(version, rules.entity_rules)
    is_recall = version["source_kind"] == "recall_api"
    identity_fields = ["Make", "Model", "ModelYear", "Component", "NHTSACampaignNumber"]
    if not is_recall:
        identity_fields = ["odiNumber"]
        for index, product in enumerate(version["payload"]["products"]):
            identity_fields.extend(
                f"products/{index}/{field}"
                for field in ("productMake", "productModel", "productYear")
                if isinstance(product.get(field), str) and product[field]
            )
        if version["payload"].get("components"):
            identity_fields.append("components")
    structured_evidence = [evidence(f) for f in identity_fields]
    facts = []
    for fact in rules.facts:
        if fact.source_version_id != version["version_id"]:
            continue
        if fact.facet == "official_reference" and not is_recall:
            raise ValueError("complaint text is not an official statement of reference")
        if fact.facet in {"production_range", "configuration"} and len(projection["vehicles"]) != 1:
            raise ValueError("conditional facts require an unambiguous single vehicle position")
        facts.append(fact.model_dump(mode="json") | {"evidence": evidence(fact.field, fact.quote)})
    result = {
        "schema_version": "entity-scope/0.1",
        "source": base,
        "source_kind": version["source_kind"],
        "official_id": version["payload"].get("NHTSACampaignNumber") if is_recall else None,
        "entity_rules_version": rules.entity_rules.rules_version,
        "entity_rules_sha256": rules.entity_rules.sha256,
        "scope_rules_version": rules.rules_version,
        "scope_rules_sha256": rules.sha256,
        "vehicles": projection["vehicles"],
        "components": projection["components"],
        "structured_evidence": structured_evidence,
        "facts": facts,
        "coverage": "reviewed_facts_only_not_complete_vehicle_applicability",
    }
    return result | {"scope_id": content_hash(result)}

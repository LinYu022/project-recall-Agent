"""Explicit, versioned entity mapping; approximate names remain candidates."""

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import model_validator

from domain.tasks import Contract, Text
from ingestion.records import content_hash


class EntityRules(Contract):
    rules_version: Text
    models_by_make: dict[str, list[str]]
    component_labels: list[str]
    unknown_labels: list[str]

    @model_validator(mode="after")
    def explicit_canonical_labels(self):
        groups = [
            list(self.models_by_make),
            *self.models_by_make.values(),
            self.component_labels,
            self.unknown_labels,
        ]
        if any(
            len(group) != len(set(group)) or any(lexical(label) != label for label in group)
            for group in groups
        ):
            raise ValueError("entity rule labels must be unique and lexically canonical")
        if any(set(group) & set(self.unknown_labels) for group in groups[:-1]):
            raise ValueError("unknown labels must not also be canonical entity labels")
        return self

    @property
    def sha256(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def load_rules() -> EntityRules:
    path = Path(__file__).resolve().parents[1] / "examples/entity-rules.json"
    return EntityRules.model_validate_json(path.read_bytes())


def lexical(raw: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", raw).split()).upper()


def name_value(raw: object, allowed: list[str], rules: EntityRules) -> dict:
    value = {"raw": raw, "canonical": None, "status": "unknown", "candidates": []}
    if raw is None:
        return value
    if not isinstance(raw, str):
        return value | {"status": "invalid"}
    normalized = lexical(raw)
    if normalized in rules.unknown_labels:
        return value
    if normalized in allowed:
        return value | {"canonical": normalized, "status": "mapped"}
    scores = sorted(
        [(round(SequenceMatcher(None, normalized, label).ratio(), 4), label) for label in allowed],
        key=lambda pair: (-pair[0], pair[1]),
    )
    candidates = [{"value": label, "score": score} for score, label in scores if score >= 0.8][:3]
    return value | {"status": "candidate" if candidates else "unmapped", "candidates": candidates}


def model_year(raw: object) -> dict:
    value = {"raw": raw, "canonical": None, "status": "unknown"}
    if raw is None or raw in ("", "9999", 9999):
        return value
    if type(raw) is int:
        year = raw
    elif isinstance(raw, str) and re.fullmatch(r"\d{4}", raw):
        year = int(raw)
    else:
        return value | {"status": "invalid"}
    return value | (
        {"canonical": year, "status": "mapped"} if 1886 <= year <= 2100 else {"status": "invalid"}
    )


def project_record(version: dict, rules: EntityRules) -> dict:
    kind, payload = version["source_kind"], version["payload"]
    vehicles = []
    if kind == "complaint_api":
        products = payload.get("products", [])
        raw_components = payload.get("components")
        labels = raw_components.split(",") if isinstance(raw_components, str) else [raw_components]
        fields = ("productMake", "productModel", "productYear")
    elif kind == "recall_api":
        products = [payload]
        labels = [payload.get("Component")]
        fields = ("Make", "Model", "ModelYear")
    else:
        products, labels, fields = [], [], ("", "", "")
    for index, product in enumerate(products):
        make = name_value(product.get(fields[0]), list(rules.models_by_make), rules)
        allowed_models = rules.models_by_make.get(make["canonical"], [])
        model = name_value(product.get(fields[1]), allowed_models, rules)
        vehicles.append(
            {
                "position": index,
                "make": make,
                "model": model,
                "model_year": model_year(product.get(fields[2])),
                "production_date": {
                    "raw": None,
                    "canonical": None,
                    "status": "unknown",
                    "basis": "not_supplied_in_supported_structured_fields",
                },
                "configuration": {
                    "raw": None,
                    "canonical": None,
                    "status": "unknown",
                    "basis": "not_extracted_from_free_text",
                },
            }
        )
    return {
        "schema_version": "entity-projection/0.1",
        "source_version_id": version["version_id"],
        "rules_version": rules.rules_version,
        "rules_sha256": rules.sha256,
        "vehicles": vehicles,
        "components": [name_value(label, rules.component_labels, rules) for label in labels],
    }

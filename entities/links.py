"""Five narrow relationship types; confirmation never establishes causation."""

from pathlib import Path

from domain.tasks import Contract
from entities.scopes import RecordLocator, ScopeRules, build_scope
from ingestion.records import content_hash

LINK_RULES_VERSION = "entity-link/0.1"
# Hash the semantic policy as well as naming it. Policy changes require a new version.
POLICY = {
    "version": LINK_RULES_VERSION,
    "scope": "exact_make_model_year_and_no_known_condition_conflict",
    "dates": "inclusive_intersection_unknown_is_not_inside",
    "configuration": "exact_reviewed_values_unknown_is_not_equal",
    "symptom": "common_reviewed_tag_not_common_component_alone",
    "reference": "directional_reviewed_explicit_identifier_mention_only",
    "exclusion": "known_disjoint_vehicle_tuple_date_or_configuration",
    "causation": "never_established",
}


class LinkRequest(Contract):
    left: RecordLocator
    right: RecordLocator


def _facts(scope: dict, facet: str) -> dict:
    return {f["key"]: f["value"] for f in scope["facts"] if f["facet"] == facet}


def _vehicle_tuples(scope: dict) -> tuple[set, bool]:
    values = {
        tuple(v[field]["canonical"] for field in ("make", "model", "model_year"))
        for v in scope["vehicles"]
    }
    return {v for v in values if None not in v}, not values or any(None in v for v in values)


def _assess(left: dict, right: dict) -> dict:
    left_tuples, left_unknown = _vehicle_tuples(left)
    right_tuples, right_unknown = _vehicle_tuples(right)
    shared = sorted(left_tuples & right_tuples)
    conflicts, unknown, matched = [], [], []
    if shared:
        matched.append("make_model_year")
    elif left_unknown or right_unknown:
        unknown.append("make_model_year")
    else:
        conflicts.append("make_model_year")

    lp = _facts(left, "production_range").get("production_range")
    rp = _facts(right, "production_range").get("production_range")
    if lp is None or rp is None:
        unknown.append("production_range")
    elif max(lp["start"], rp["start"]) <= min(lp["end"], rp["end"]):
        matched.append("production_range")
    else:
        conflicts.append("production_range")
    lc, rc = _facts(left, "configuration"), _facts(right, "configuration")
    for key in sorted(lc.keys() | rc.keys()):
        name = "configuration:" + key
        if key not in lc or key not in rc:
            unknown.append(name)
        elif lc[key] == rc[key]:
            matched.append(name)
        else:
            conflicts.append(name)
    # API scopes use "certain vehicles"; these fields never prove VIN membership or completeness.
    unknown.extend(["unreviewed_configuration_conditions", "individual_vehicle_membership"])
    scope_status = "rejected" if conflicts else "confirmed" if shared else "candidate"
    common_symptoms = sorted(
        set(_facts(left, "symptom").values()) & set(_facts(right, "symptom").values())
    )
    references = _facts(left, "official_reference")
    target = right["official_id"]
    referenced = target is not None and target in references.values()
    evidence = [
        *left["structured_evidence"],
        *right["structured_evidence"],
        *[f["evidence"] for f in left["facts"] + right["facts"]],
    ]
    statuses = {
        "scope_overlap": (
            scope_status,
            "Documented make/model/year scopes overlap; no known reviewed condition conflict. "
            "Individual vehicle applicability remains unconfirmed.",
        ),
        "symptom_similar": (
            "confirmed" if common_symptoms else "candidate",
            "Reviewed symptom descriptions share a tag; defect identity and causation are unconfirmed.",
        ),
        "officially_referenced": (
            "confirmed" if referenced else "candidate",
            "The left source explicitly mentions the right official identifier. "
            "This confirms the mention only, not endorsement or causation.",
        ),
        "excluded_by_scope": (
            "confirmed" if conflicts else "candidate",
            "The two documented scopes are disjoint on a known condition. "
            "This does not establish an absence of defects.",
        ),
        "applicability_unknown": (
            "rejected" if conflicts else "confirmed",
            "Available conditions cannot establish complete applicability; retain the listed gaps.",
        ),
    }
    links = []
    for relation, (status, wording) in statuses.items():
        if relation == "symptom_similar":
            supporting = [
                f["evidence"]
                for f in left["facts"] + right["facts"]
                if f["facet"] == "symptom" and f["value"] in common_symptoms
            ]
        elif relation == "officially_referenced":
            supporting = [
                f["evidence"]
                for f in left["facts"]
                if f["facet"] == "official_reference" and f["value"] == target
            ] + [e for e in right["structured_evidence"] if e["field"] == "NHTSACampaignNumber"]
        else:
            supporting = [
                *left["structured_evidence"],
                *right["structured_evidence"],
                *[
                    f["evidence"]
                    for f in left["facts"] + right["facts"]
                    if f["facet"] in {"production_range", "configuration"}
                ],
            ]
        stage = (
            "unknown"
            if relation == "applicability_unknown" and status == "confirmed"
            else "excluded"
            if relation == "excluded_by_scope" and status == "confirmed"
            else status
        )
        link = {
            "relation_type": relation,
            "confirmation_status": status,
            "stage": stage,
            "left": {"scope_id": left["scope_id"], **left["source"]},
            "right": {"scope_id": right["scope_id"], **right["source"]},
            "matching_method": POLICY,
            "conditions": {"matched": matched, "conflicts": conflicts},
            "unknown_conditions": unknown,
            "common_vehicle_tuples": shared,
            "common_symptom_tags": common_symptoms,
            "evidence": evidence,
            "supporting_evidence": supporting if status == "confirmed" else [],
            "allowed_wording": wording if status == "confirmed" else None,
            "causation": "not_established",
        }
        links.append(link | {"link_id": content_hash(link)})
    result = {
        "schema_version": "entity-link-assessment/0.1",
        "status": "assessed",
        "link_rules_version": LINK_RULES_VERSION,
        "link_rules_sha256": content_hash(POLICY),
        "scope_rules_version": left["scope_rules_version"],
        "scope_rules_sha256": left["scope_rules_sha256"],
        "dataset_release": None,
        "time_mode": "snapshot_only",
        "temporal_limit": "No historical cutoff claim; use temporal selection before task use.",
        "left_scope": left,
        "right_scope": right,
        "links": links,
    }
    return result | {"assessment_id": content_hash(result)}


def assess_links(root: Path, request: LinkRequest, rules: ScopeRules) -> dict:
    # The public entry point rebuilds profiles and checks source integrity, never trusts caller facts.
    left = build_scope(root, request.left, rules)
    right = build_scope(root, request.right, rules)
    if left["source"]["source_version_id"] == right["source"]["source_version_id"]:
        raise ValueError("relationship assessment requires two different source versions")
    return _assess(left, right)


def save_assessment(root: Path, assessment: dict) -> Path:
    """Content-addressed artifacts preserve both evidence endpoints and rule fingerprints."""
    import json

    digest = assessment["assessment_id"]
    payload = {k: v for k, v in assessment.items() if k != "assessment_id"}
    if content_hash(payload) != digest:
        raise ValueError("assessment fingerprint differs from content")
    root.mkdir(parents=True, exist_ok=True)
    # Keep a version name from being reused with different content in this artifact store.
    receipts = [
        ("link", assessment["link_rules_version"], assessment["link_rules_sha256"]),
        ("scope", assessment["scope_rules_version"], assessment["scope_rules_sha256"]),
        (
            "entity",
            assessment["left_scope"]["entity_rules_version"],
            assessment["left_scope"]["entity_rules_sha256"],
        ),
    ]
    registry = root / "rules"
    registry.mkdir(exist_ok=True)
    for kind, version, sha256 in receipts:
        receipt = json.dumps({"kind": kind, "version": version, "sha256": sha256}, sort_keys=True)
        receipt_path = registry / (content_hash([kind, version]) + ".json")
        try:
            with receipt_path.open("x", encoding="utf-8") as stream:
                stream.write(receipt)
        except FileExistsError:
            if receipt_path.read_text(encoding="utf-8") != receipt:
                raise ValueError("rules changed without a new version name") from None
    path = root / (digest + ".json")
    encoded = json.dumps(assessment, ensure_ascii=False, indent=2) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("existing assessment artifact differs") from None
    return path

"""Synthetic evidence and failures; these are mechanism checks, not model experiments."""

import json

import pytest
from pydantic import ValidationError

from entities.__main__ import main
from entities.links import LinkRequest, assess_links, save_assessment
from entities.normalize import load_rules
from entities.scopes import RecordLocator, ScopeRules, build_scope
from ingestion.contracts import SourceSpec
from ingestion.records import content_hash, prepare_import
from ingestion.snapshots import FetchResult, SnapshotStore
from runtime.artifacts import ArtifactIntegrityError
from tests.source_fixtures import capture_rows, complaint, recall


def source(root, campaign, **changes):
    row = (
        recall()
        | {
            "NHTSACampaignNumber": campaign,
            "Make": "FORD",
            "Model": "F-150",
            "ModelYear": "2016",
            "Component": "SERVICE BRAKES",
            "Summary": "SYNTHETIC " + campaign,
        }
        | changes
    )
    body = json.dumps({"Count": 1, "results": [row]}).encode()
    spec = SourceSpec(
        source_id="SYNTHETIC-" + campaign,
        kind="recall_api",
        url="https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=" + campaign,
        selection={"campaign_number": campaign},
    )
    snapshot = SnapshotStore(root).capture(spec, lambda _: FetchResult(200, {}, body))
    version = prepare_import(root, snapshot.snapshot_id).rows[0].version
    return RecordLocator(snapshot_id=snapshot.snapshot_id, ordinal=0), version


def fact(version, facet, value, quote, key=None, field="Summary"):
    return dict(
        source_version_id=version["version_id"],
        facet=facet,
        key=key or facet,
        value=value,
        field=field,
        quote=quote,
        review_note="SYNTHETIC reviewed rule",
    )


def rules(*facts, version="synthetic-scope/0.1"):
    return ScopeRules.model_validate(
        dict(
            rules_version=version,
            entity_rules=load_rules().model_dump(),
            configuration_domains={"engine": ["3.5L ECOBOOST", "5.0L V8"]},
            facts=list(facts),
        )
    )


def decisions(result):
    return {link["relation_type"]: link for link in result["links"]}


def test_reviewed_production_range_retains_raw_quote_and_version(tmp_path):
    quote = "built 2006-08-18 through 2010-09-11"
    loc, ver = source(tmp_path, "20V332000", Summary="SYNTHETIC " + quote)
    r = rules(fact(ver, "production_range", dict(start="2006-08-18", end="2010-09-11"), quote))
    scope = build_scope(tmp_path, loc, r)
    evidence = scope["facts"][0]["evidence"]
    assert ver["payload"]["Summary"][evidence["start"] : evidence["end"]] == quote
    assert evidence["source_version_id"] == ver["version_id"]
    assert scope["vehicles"][0]["production_date"]["canonical"] is None
    assert scope["facts"][0]["value"]["start"] == "2006-08-18"


def test_rule_for_old_version_does_not_apply_to_new_summary(tmp_path):
    old_loc, old = source(tmp_path, "20V332000", Summary="SYNTHETIC 3.5L ECOBOOST")
    new_loc, new = source(tmp_path, "20V332000", Summary="SYNTHETIC changed 3.5L ECOBOOST")
    r = rules(fact(old, "configuration", "3.5L ECOBOOST", "3.5L ECOBOOST", key="engine"))
    assert build_scope(tmp_path, old_loc, r)["facts"]
    assert build_scope(tmp_path, new_loc, r)["facts"] == []
    assert new["version_id"] != old["version_id"]


@pytest.mark.parametrize("quote", ["absent", "SYNTHETIC SYNTHETIC"])
def test_nonmatching_quote_is_rejected(tmp_path, quote):
    loc, ver = source(tmp_path, "20V332000")
    with pytest.raises(ValueError, match="exactly once"):
        build_scope(tmp_path, loc, rules(fact(ver, "symptom", "TAG", quote)))


def test_ambiguous_quote_is_rejected(tmp_path):
    loc, ver = source(tmp_path, "20V332000", Summary="SYNTHETIC door door")
    with pytest.raises(ValueError, match="exactly once"):
        build_scope(tmp_path, loc, rules(fact(ver, "symptom", "TAG", "door")))


def test_unknown_production_and_config_do_not_confirm_individual_applicability(tmp_path):
    left, _ = source(tmp_path, "17V331000")
    right, _ = source(tmp_path, "17V652000")
    d = decisions(assess_links(tmp_path, LinkRequest(left=left, right=right), rules()))
    assert d["scope_overlap"]["confirmation_status"] == "confirmed"
    assert d["applicability_unknown"]["stage"] == "unknown"
    assert "production_range" in d["applicability_unknown"]["unknown_conditions"]
    assert d["symptom_similar"]["confirmation_status"] == "candidate"
    assert all(link["causation"] == "not_established" for link in d.values())


@pytest.mark.parametrize("other_start,excluded", [("2010-09-11", False), ("2010-09-12", True)])
def test_inclusive_production_boundary_and_disjoint_ranges(tmp_path, other_start, excluded):
    left, lv = source(tmp_path, "17V331000", Summary="SYNTHETIC range left")
    right, rv = source(tmp_path, "17V652000", Summary="SYNTHETIC range right")
    r = rules(
        fact(lv, "production_range", dict(start="2006-08-18", end="2010-09-11"), "range left"),
        fact(rv, "production_range", dict(start=other_start, end="2011-01-01"), "range right"),
    )
    d = decisions(assess_links(tmp_path, LinkRequest(left=left, right=right), r))
    assert (d["excluded_by_scope"]["confirmation_status"] == "confirmed") is excluded
    if excluded:
        assert d["scope_overlap"]["confirmation_status"] == "rejected"
        assert "production_range" in d["excluded_by_scope"]["conditions"]["conflicts"]


@pytest.mark.parametrize("mode", ["missing", "equal", "different"])
def test_configuration_requires_explicit_reviewed_domain_values(tmp_path, mode):
    left, lv = source(tmp_path, "17V331000", Summary="SYNTHETIC 3.5L ECOBOOST")
    other = "5.0L V8" if mode == "different" else "3.5L ECOBOOST"
    right, rv = source(tmp_path, "17V652000", Summary="SYNTHETIC " + other)
    facts = [fact(lv, "configuration", "3.5L ECOBOOST", "3.5L ECOBOOST", key="engine")]
    if mode != "missing":
        facts.append(fact(rv, "configuration", other, other, key="engine"))
    d = decisions(assess_links(tmp_path, LinkRequest(left=left, right=right), rules(*facts)))
    assert (d["excluded_by_scope"]["stage"] == "excluded") is (mode == "different")
    assert ("configuration:engine" in d["scope_overlap"]["unknown_conditions"]) is (
        mode == "missing"
    )


def test_fuzzy_model_is_candidate_not_scope_confirmation(tmp_path):
    left, _ = source(tmp_path, "17V331000", Model="F150")
    right, _ = source(tmp_path, "17V652000")
    result = assess_links(tmp_path, LinkRequest(left=left, right=right), rules())
    assert decisions(result)["scope_overlap"]["confirmation_status"] == "candidate"
    assert result["left_scope"]["vehicles"][0]["model"]["candidates"]


def test_known_year_mismatch_excludes_even_when_other_conditions_unknown(tmp_path):
    left, _ = source(tmp_path, "17V331000", ModelYear="2015")
    right, _ = source(tmp_path, "17V652000")
    d = decisions(assess_links(tmp_path, LinkRequest(left=left, right=right), rules()))
    assert d["excluded_by_scope"]["stage"] == "excluded"
    assert d["applicability_unknown"]["confirmation_status"] == "rejected"


def test_matching_components_alone_do_not_establish_symptoms_or_cause(tmp_path):
    left, _ = source(tmp_path, "17V331000")
    right, _ = source(tmp_path, "17V652000")
    result = assess_links(tmp_path, LinkRequest(left=left, right=right), rules())
    d = decisions(result)
    assert d["symptom_similar"]["supporting_evidence"] == []
    assert d["officially_referenced"]["confirmation_status"] == "candidate"


def test_common_reviewed_symptom_is_narrow_confirmation(tmp_path):
    left, lv = source(tmp_path, "17V331000", Summary="SYNTHETIC door opens under impact")
    right, rv = source(tmp_path, "17V652000", Summary="SYNTHETIC frozen latch door opens")
    r = rules(
        fact(lv, "symptom", "DOOR_OPEN", "door opens"),
        fact(rv, "symptom", "DOOR_OPEN", "door opens"),
    )
    link = decisions(assess_links(tmp_path, LinkRequest(left=left, right=right), r))[
        "symptom_similar"
    ]
    assert link["confirmation_status"] == "confirmed"
    assert len(link["supporting_evidence"]) == 2
    assert link["causation"] == "not_established"


def test_explicit_official_mention_is_directional_and_does_not_prove_endorsement(tmp_path):
    left, lv = source(tmp_path, "17V331000", Summary="SYNTHETIC not covered by 17V652000")
    right, _ = source(tmp_path, "17V652000")
    r = rules(fact(lv, "official_reference", "17V652000", "not covered by 17V652000"))
    link = decisions(assess_links(tmp_path, LinkRequest(left=left, right=right), r))[
        "officially_referenced"
    ]
    reverse = decisions(assess_links(tmp_path, LinkRequest(left=right, right=left), r))
    assert link["confirmation_status"] == "confirmed"
    assert "mention only" in link["allowed_wording"]
    assert reverse["officially_referenced"]["confirmation_status"] == "candidate"


def test_reviewed_short_recall_number_preserves_original_identifier(tmp_path):
    left, lv = source(tmp_path, "17V331000", Summary="SYNTHETIC expansion of 20V-332")
    right, _ = source(tmp_path, "20V332000")
    reviewed = fact(lv, "official_reference", "20V332000", "expansion of 20V-332")
    reviewed["mentioned_identifier"] = "20V-332"
    result = assess_links(tmp_path, LinkRequest(left=left, right=right), rules(reviewed))
    assert decisions(result)["officially_referenced"]["confirmation_status"] == "confirmed"
    assert result["left_scope"]["facts"][0]["mentioned_identifier"] == "20V-332"
    reviewed["value"] = "19V332000"
    with pytest.raises(ValidationError):
        rules(reviewed)


def test_rules_reject_conflict_invalid_range_unregistered_config_and_fake_reference(tmp_path):
    _, ver = source(tmp_path, "17V331000")
    f = fact(ver, "symptom", "TAG", "SYNTHETIC")
    for facts in [
        [f, f],
        [fact(ver, "production_range", dict(start="2011-01-01", end="2010-01-01"), "x")],
        [fact(ver, "configuration", "UNKNOWN ENGINE", "x", key="engine")],
        [fact(ver, "official_reference", "17V652000", "not an identifier")],
    ]:
        with pytest.raises(ValidationError):
            rules(*facts)


def test_tampered_source_and_invalid_ordinal_and_self_link_are_rejected(tmp_path):
    loc, _ = source(tmp_path, "17V331000")
    with pytest.raises(ValueError, match="ordinal"):
        build_scope(tmp_path, loc.model_copy(update={"ordinal": 9}), rules())
    with pytest.raises(ValueError, match="different source versions"):
        assess_links(tmp_path, LinkRequest(left=loc, right=loc), rules())
    snapshot = SnapshotStore(tmp_path).verify(loc.snapshot_id)
    (tmp_path / "objects" / snapshot.raw.sha256).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        build_scope(tmp_path, loc, rules())


def test_artifacts_are_idempotent_immutable_and_rule_changes_require_new_name(tmp_path):
    left, _ = source(tmp_path, "17V331000")
    right, _ = source(tmp_path, "17V652000")
    request = LinkRequest(left=left, right=right)
    result = assess_links(tmp_path, request, rules())
    target = tmp_path / "artifacts"
    path = save_assessment(target, result)
    assert save_assessment(target, result) == path
    altered = rules().model_dump()
    altered["entity_rules"]["component_labels"].append("SYNTHETIC NEW LABEL")
    drift = assess_links(tmp_path, request, ScopeRules.model_validate(altered))
    with pytest.raises(ValueError, match="new version name"):
        save_assessment(target, drift)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact differs"):
        save_assessment(target, result)
    with pytest.raises(ValueError, match="fingerprint"):
        save_assessment(target, result | {"assessment_id": "0" * 64})
    assert result["assessment_id"] == content_hash(
        {k: v for k, v in result.items() if k != "assessment_id"}
    )


def test_link_cli_works_without_database_and_refuses_missing_snapshot(
    tmp_path, monkeypatch, capsys
):
    left, _ = source(tmp_path, "17V331000")
    right, _ = source(tmp_path, "17V652000")
    request = tmp_path / "request.json"
    request.write_text(LinkRequest(left=left, right=right).model_dump_json(), encoding="utf-8")
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    monkeypatch.setattr("entities.__main__.load_scope_rules", rules)
    argv = [
        "entities",
        "link",
        str(request),
        "--root",
        str(tmp_path),
        "--artifacts",
        str(tmp_path / "artifacts"),
    ]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["causation"] == "not_established"
    (tmp_path / "snapshots" / (right.snapshot_id + ".json")).unlink()
    assert main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "entity_operation_failed"


def test_complaint_nested_vehicle_evidence_preserves_real_pointer(tmp_path):
    snapshot = capture_rows(tmp_path, [complaint()])
    scope = build_scope(
        tmp_path, RecordLocator(snapshot_id=snapshot.snapshot_id, ordinal=0), rules()
    )
    ev = next(e for e in scope["structured_evidence"] if e["field"] == "products/0/productModel")
    assert ev["field_pointer"] == "/results/0/products/0/productModel"
    assert ev["quote"] == "F-150"
    assert scope["official_id"] is None


def test_complaint_mention_cannot_become_an_official_reference(tmp_path):
    snapshot = capture_rows(tmp_path, [complaint(summary="SYNTHETIC mentions 20V332000")])
    ver = prepare_import(tmp_path, snapshot.snapshot_id).rows[0].version
    r = rules(fact(ver, "official_reference", "20V332000", "20V332000", field="summary"))
    with pytest.raises(ValueError, match="not an official statement"):
        build_scope(tmp_path, RecordLocator(snapshot_id=snapshot.snapshot_id, ordinal=0), r)


def test_condition_cannot_be_silently_applied_to_multiple_product_positions(tmp_path):
    row = complaint(summary="SYNTHETIC 3.5L ECOBOOST")
    row["products"].append({"productMake": "FORD", "productModel": "F-150", "productYear": "2015"})
    snapshot = capture_rows(tmp_path, [row])
    ver = prepare_import(tmp_path, snapshot.snapshot_id).rows[0].version
    r = rules(
        fact(ver, "configuration", "3.5L ECOBOOST", "3.5L ECOBOOST", key="engine", field="summary")
    )
    with pytest.raises(ValueError, match="single vehicle position"):
        build_scope(tmp_path, RecordLocator(snapshot_id=snapshot.snapshot_id, ordinal=0), r)

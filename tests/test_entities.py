"""Offline tests use controlled labels; mapping is not a causal finding."""

import json

import pytest
from pydantic import ValidationError

from entities.__main__ import main
from entities.counts import ComplaintCountRequest, count_complaints
from entities.normalize import EntityRules, load_rules, model_year, name_value, project_record
from tests.source_fixtures import capture_rows, complaint, recall


def projected(payload, kind="complaint_api"):
    return project_record(
        {"version_id": "a" * 64, "source_kind": kind, "payload": payload}, load_rules()
    )


def test_lexical_normalization_preserves_original_and_requires_known_model():
    raw = "  ｆｏｒｄ  "
    rules = load_rules()
    assert name_value(raw, list(rules.models_by_make), rules)["canonical"] == "FORD"
    assert name_value(raw, list(rules.models_by_make), rules)["raw"] == raw


def test_fuzzy_name_only_produces_a_candidate():
    value = name_value("F150", ["F-150"], load_rules())
    assert value["status"] == "candidate" and value["canonical"] is None
    assert value["candidates"][0]["value"] == "F-150"
    assert name_value("HS250H", ["HS"], load_rules())["canonical"] is None


@pytest.mark.parametrize(
    "raw,canonical,status",
    [
        ("2016", 2016, "mapped"),
        (2016, 2016, "mapped"),
        (None, None, "unknown"),
        ("9999", None, "unknown"),
        (True, None, "invalid"),
        (2016.0, None, "invalid"),
        ("2016/2017", None, "invalid"),
        ("2200", None, "invalid"),
    ],
)
def test_model_year_is_strict_and_keeps_unknown_separate(raw, canonical, status):
    assert model_year(raw) == {"raw": raw, "canonical": canonical, "status": status}


def test_component_delimiters_are_source_specific():
    result = projected(complaint() | {"components": "POWER TRAIN,SERVICE BRAKES,UNKNOWN OR OTHER"})
    assert [c["canonical"] for c in result["components"]] == ["POWER TRAIN", "SERVICE BRAKES", None]
    result = projected(
        recall() | {"Component": "SERVICE BRAKES, AIR:ANTILOCK:CONTROL UNIT/MODULE"}, "recall_api"
    )
    assert len(result["components"]) == 1
    assert result["components"][0]["canonical"] != "SERVICE BRAKES"


def test_missing_production_and_config_are_not_guessed_from_model_year_or_summary():
    result = projected(complaint(summary="SYNTHETIC mentions 3.5L and built 2016-01-01"))
    vehicle = result["vehicles"][0]
    assert vehicle["production_date"]["canonical"] is None
    assert vehicle["configuration"]["canonical"] is None
    assert vehicle["model_year"]["canonical"] == 2016


def test_rules_reject_ambiguous_unknown_and_noncanonical_labels():
    values = load_rules().model_dump()
    with pytest.raises(ValidationError):
        EntityRules.model_validate(values | {"component_labels": ["UNKNOWN OR OTHER"]})
    with pytest.raises(ValidationError):
        EntityRules.model_validate(values | {"models_by_make": {"ford": ["F-150"]}})


def test_inspection_does_not_require_database_or_print_source_summary(
    tmp_path, monkeypatch, capsys
):
    snapshot = capture_rows(tmp_path, [complaint() | {"components": "POWER TRAIN,SERVICE BRAKES"}])
    monkeypatch.delenv("RECALL_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["entities", "inspect", snapshot.snapshot_id, "--root", str(tmp_path)]
    )
    assert main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)["component_positions"] == 2
    assert "SYNTHETIC COMPLAINT" not in output


def test_historical_refusal_has_no_count_and_does_not_open_database(tmp_path):
    snapshot = capture_rows(tmp_path, [complaint() | {"components": "SERVICE BRAKES"}])
    body = {
        "temporal": {
            "snapshot_ids": [snapshot.snapshot_id],
            "time": {
                "mode": "historical_as_of",
                "event_interval": None,
                "knowledge_cutoff": "2000-01-01T00:00:00Z",
                "date_filter_basis": "Controlled historical refusal",
            },
        },
        "make": "FORD",
        "model": "F-150",
        "model_year": 2016,
    }

    class NoDatabase:
        def connect(self):
            pytest.fail("unsupported historical count must not query database")

    result = count_complaints(
        NoDatabase(), tmp_path, ComplaintCountRequest.model_validate(body), load_rules()
    )
    assert result["status"] == "unsupported" and result["distinct_complaints"] is None

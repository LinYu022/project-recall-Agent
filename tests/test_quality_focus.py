from copy import deepcopy

import pytest

from quality_focus.workflow import validate


def sample():
    payload = {
        "mode": "claims",
        "selected_claims": [
            {"claim_id": "C1", "statement": "部分车辆召回。", "support_ids": ["E1"]}
        ],
        "evidence_groups": [
            {"aliases": ["E1"], "payload": {"text": "certain vehicles are recalled"}}
        ],
    }
    value = {
        "mode": "claims",
        "claims": [
            {
                "claim_id": "C1",
                "support_verdict": "supported",
                "scope_verdict": "correct",
                "qualifier_preserved": True,
                "units": [
                    {
                        "quote": "部分车辆召回。",
                        "verdict": "supported",
                        "evidence_alias": "E1",
                        "source_quote": "certain vehicles are recalled",
                        "reason": "SYNTHETIC",
                    }
                ],
                "omissions": [],
                "reason": "SYNTHETIC",
            }
        ],
    }
    return payload, value


def test_exact_focused_quote_and_actual_citation():
    payload, value = sample()
    assert validate(payload, value) == value
    invented = deepcopy(value)
    invented["claims"][0]["units"][0]["source_quote"] = "all vehicles permanently safe"
    with pytest.raises(ValueError, match="source_quote_not_exact"):
        validate(payload, invented)
    invented = deepcopy(value)
    invented["claims"][0]["units"][0]["evidence_alias"] = "E2"
    with pytest.raises(ValueError, match="adds_uncited"):
        validate(payload, invented)


def test_guard_stage_never_confirms_completed_individual_investigation():
    payload = {"mode": "stages", "cases": [{"blind_id": "B1"}]}
    value = {
        "mode": "stages",
        "cases": [
            {
                "blind_id": "B1",
                "stage_appropriate": True,
                "stage_requirements_satisfied": True,
                "full_individual_or_historical_investigation_complete": False,
            }
        ],
    }
    assert validate(payload, value) == value
    value["cases"][0]["full_individual_or_historical_investigation_complete"] = True
    with pytest.raises(ValueError, match="stage_invents_complete"):
        validate(payload, value)

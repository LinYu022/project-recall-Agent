from copy import deepcopy

import pytest

from evaluation.contracts import Protocol, cells, request_ceiling
from reviewing.corpus import read
from scripts.run_quality_confirmation import validate_approval


def test_frozen_confirmation_keeps_all_methods_targets_and_human_sample():
    protocol = Protocol.model_validate(read("evals/quality-confirmation-v0.1/protocol.json"))
    units = cells(protocol)
    assert len(units) == 140 and request_ceiling(protocol) == 375
    assert {a.method for a in protocol.arms} == {
        "fixed",
        "single_agent",
        "parallel_tools",
        "multi_agent",
    }
    assert set(protocol.quality_targets["execution_order"]) == {c["cell_id"] for c in units}
    selected = set(protocol.quality_targets["mandatory_human_cells"])
    assert len(selected) == 80
    assert {c["cell_id"] for c in units if c["method"] == "fixed"} <= selected
    assert (
        protocol.quality_targets["prospective_numerical"][
            "atomic_unsupported_event_cluster_mean_maximum"
        ]
        == 0
    )
    assert protocol.quality_targets["model_judge_status"].startswith("failed_calibration")
    assert not protocol.quality_targets["old_tests_retroactively_preregistered"]


def test_reference_revisions_are_scoring_side_and_bmw_uses_2022():
    protocol = read("evals/quality-confirmation-v0.1/protocol.json")
    bmw = next(c for c in protocol["cases"] if c["case_id"] == "QTEST-24V104000-YEAR")
    assert bmw["scope"]["model_year"] == 2022
    hyundai = read("evals/quality-confirmation-v0.1/references/QTEST-25V065000-FACTS.json")
    remedy = next(
        o for o in hyundai["required_claims"] if o["reference_claim_id"] == "R-25V065000-REMEDY"
    )
    assert "previously repaired" in remedy["statement"] and "N E-shift" in remedy["statement"]
    for case in protocol["cases"]:
        assert "required_claims" not in case["request"]
    stage = protocol["quality_targets"]["stage_obligations"]
    assert stage["QTEST-24V744000-PAIR"][0]["obligation_id"] == "R-INSUFFICIENT-CLOSURE-EVIDENCE"


def test_only_matching_approval_contract_enables_generation():
    # Synthetic validator fixture only: no provider, actual user reply or saved
    # authorization is read or written by this ordinary engineering test.
    identity = {"experiment_id": "a" * 64}
    protocol = Protocol.model_validate(read("evals/quality-confirmation-v0.1/protocol.json"))
    approval = {
        "experiment_id": identity["experiment_id"],
        "maximum_requests": request_ceiling(protocol),
        "status": "explicit_user_approved",
        "source_kind": "actual_human_reply",
        "human_reply": "SYNTHETIC CONTRACT FIXTURE ONLY; not an actual authorization",
        "model": protocol.model_id,
        "endpoint": protocol.endpoint,
    }
    validate_approval(approval, identity, protocol)
    wrong = deepcopy(approval)
    wrong["experiment_id"] = "0" * 64
    with pytest.raises(ValueError, match="no_matching_authorization"):
        validate_approval(wrong, identity, protocol)
    with pytest.raises(ValueError, match="no_matching_authorization"):
        validate_approval(
            {"publication_status": "private_audit_not_distributed"}, identity, protocol
        )

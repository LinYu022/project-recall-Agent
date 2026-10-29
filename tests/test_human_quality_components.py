from copy import deepcopy

import pytest

from scripts.summarize_human_quality_components import bind_atoms


def sample():
    packet = {
        "items": [
            {
                "case_id": "DEV-X",
                "packet_sha256": "a" * 64,
                "claims": [
                    {
                        "claim": {
                            "claim_id": "C001",
                            "statement": "在条件甲下，出现现象乙。",
                            "support_ids": ["E001"],
                        },
                        "candidate_units": [
                            {"atom_id": "C001-A01", "start": 0, "end": 6, "quote": "在条件甲下，"},
                            {"atom_id": "C001-A02", "start": 6, "end": 12, "quote": "出现现象乙。"},
                        ],
                    }
                ],
            }
        ]
    }
    human = {
        "case_id": "DEV-X",
        "adopted_atoms": [
            {
                "atom_id": "C001-N01",
                "navigation_ids": ["C001-A01", "C001-A02"],
                "verdict": "supported",
                "support_ids": ["E001"],
            }
        ],
        "assertion_coverage_exhaustive": True,
        "original_navigation_judgments": {
            "C001-A01": "partial_requires_merge",
            "C001-A02": "partial_requires_condition",
        },
        "obligations": {},
        "all_requirements_verdict": "satisfied",
    }
    return packet, human


def test_human_requested_context_merge_preserves_fragment_labels():
    packet, human = sample()
    bound = bind_atoms(human, packet)
    assert bound["atom_counts"] == {"supported": 1}
    assert bound["original_candidate_judgments"]["C001-A01"] == "partial_requires_merge"
    assert bound["atoms"][0]["quote"] == "在条件甲下，出现现象乙。"


def test_component_cannot_drop_unreviewed_fragment_or_add_evidence():
    packet, human = sample()
    changed = deepcopy(human)
    changed["adopted_atoms"][0]["navigation_ids"] = ["C001-A01"]
    with pytest.raises(ValueError, match="coverage_missing"):
        bind_atoms(changed, packet)
    changed = deepcopy(human)
    changed["adopted_atoms"][0]["support_ids"] = ["E002"]
    with pytest.raises(ValueError, match="adds_uncited_evidence"):
        bind_atoms(changed, packet)

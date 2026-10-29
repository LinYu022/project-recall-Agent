"""Small synthetic three-event release and complete experiment, with no provider calls."""

import json

import pytest

from evaluation.contracts import Protocol
from ingestion.records import content_hash
from investigation.contracts import ReferenceCase
from releases.catalog import Catalog
from tests.release_fixtures import release_factory as release_factory
from tests.test_release_postgres import publish


def protocol_data(release="a" * 64, manifest="b" * 64, source="SYNTHETIC", campaign="26V999003"):
    cases, references = [], {}
    for identifier, category, intent, year in [
        ("EVAL-FACTS", "direct", "record_lookup", 2010),
        ("EVAL-MISSING", "missing_conditions", "applicability", 2010),
        ("EVAL-YEAR", "scope_exclusion", "record_lookup", 2009),
    ]:
        ref = ReferenceCase.model_validate(
            {
                "case_id": identifier,
                "split": "test",
                "slots": [],
                "required_claims": [],
                "acceptable_dispositions": ["insufficient_evidence"]
                if category == "direct"
                else ["needs_clarification"]
                if category == "missing_conditions"
                else ["no_matching_records"],
            }
        ).model_dump(mode="json")
        references[identifier] = ref
        cases.append(
            {
                "case_id": identifier,
                "event_cluster_id": "SYNTHETIC-C",
                "split": "test",
                "category": category,
                "sources": [source],
                "scope": {
                    "make": "TOYOTA",
                    "model": "PRIUS",
                    "model_year": year,
                    "campaign_number": campaign,
                },
                "request": {
                    "task_id": identifier,
                    "intent": intent,
                    "question": "SYNTHETIC evaluation brake scope",
                    "retrieval_query": "brake",
                },
                "reference_sha256": content_hash(ref),
            }
        )
    prefix = {
        "declarations": [
            {
                "kind": "preference",
                "scope": "project",
                "semantic_key": "output.format",
                "value": "SYNTHETIC Chinese unknowns",
            }
        ],
        "origin_scopes": [
            {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010, "campaign_number": code}
            for code in ["26V999001", "26V999002"]
        ],
        "origin_source_ids": ["SYNTHETIC-CAMPAIGN-" + code for code in ["26V999001", "26V999002"]],
    }
    arms = [
        {
            "arm_id": method,
            "family": "main",
            "method": method,
            "cases": [c["case_id"] for c in cases],
            "retrieval": "bm25",
        }
        for method in ["fixed", "single_agent", "parallel_tools", "multi_agent"]
    ]
    arms += [
        {
            "arm_id": "context_" + mode,
            "family": "context",
            "method": "single_agent",
            "cases": ["EVAL-FACTS"],
            "context": mode,
            "retrieval": "bm25",
        }
        for mode in ["full_history", "recent_window", "structured"]
    ]
    arms += [
        {
            "arm_id": "memory_" + mode,
            "family": "memory_frozen",
            "method": "single_agent",
            "cases": ["EVAL-FACTS"],
            "memory": mode,
            "retrieval": "bm25",
        }
        for mode in ["none", "semantic", "semantic_episode", "all"]
    ]
    protocol = {
        "dataset_release_id": release,
        "manifest_sha256": manifest,
        "cases": cases,
        "arms": arms,
        "profile": {
            "max_model_calls": 8,
            "max_tool_calls": 10,
            "total_generation_tokens": 4000,
            "retrieval_method": "bm25",
            "context_mode": "structured",
            "prompt_characters": 32000,
        },
        "previously_seen_campaigns": ["10V039000"],
        "cluster_campaigns": {"SYNTHETIC-C": [campaign]},
        "frozen_prefix_sha256": content_hash(prefix),
        "quality_targets": {"independent_review": True},
    }
    return protocol, prefix, references


@pytest.fixture
def evaluation_data(database, release_factory, tmp_path):
    bundle = release_factory(
        "SYNTHETIC-EVALUATION", campaign="26V999003", also_campaigns=["26V999001", "26V999002"]
    )
    catalog = Catalog(database, bundle["store"])
    publish(catalog, bundle)
    protocol, prefix, references = protocol_data(
        bundle["sealed"]["release_id"],
        bundle["sealed"]["manifest_sha256"],
        "SYNTHETIC-CAMPAIGN-26V999003",
    )
    labels = tmp_path / "labels"
    (labels / "references").mkdir(parents=True)
    for identifier, ref in references.items():
        (labels / "references" / (identifier + ".json")).write_text(
            json.dumps(ref), encoding="utf-8"
        )
    return catalog, Protocol.model_validate(protocol), prefix, labels, bundle

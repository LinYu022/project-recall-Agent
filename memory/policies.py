"""Installed operating rules have bounded executable checks, not inferred domain truth."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from control.contracts import Plan, missing_conditions
from ingestion.records import content_hash
from memory.contracts import POLICIES


def verify(policy_id):
    if policy_id not in POLICIES:
        raise ValueError("uninstalled_memory_policy")
    if policy_id == "keep_applicability_unknown":
        plan = Plan.model_validate(
            {
                "release_id": "a" * 64,
                "allowed_source_ids": ["SYNTHETIC-CHECK"],
                "scope": {"make": "TOYOTA", "model": "PRIUS", "model_year": 2010},
                "request": {
                    "task_id": "SYNTHETIC",
                    "question": "SYNTHETIC",
                    "retrieval_query": "scope",
                    "intent": "applicability",
                },
            }
        )
        checks = {
            "incomplete_configuration_is_required": "configurations" in missing_conditions(plan),
            "production_date_is_required": "production_date" in missing_conditions(plan),
        }
    else:
        from investigation.evidence import verify_cards
        from runtime.artifacts import ArtifactIntegrityError

        body = {
            "kind": "structured_lookup",
            "task_id": "SYNTHETIC",
            "release_id": "a" * 64,
            "source_ids": ["SYNTHETIC"],
            "source_version_ids": [],
            "origin_sha256": "b" * 64,
        }
        card = SimpleNamespace(
            **body, evidence_id=content_hash(body), model_dump=lambda **kwargs: body
        )
        context = SimpleNamespace(
            task={"task_id": "SYNTHETIC"},
            manifest={"release_id": "a" * 64},
            sources=["SYNTHETIC"],
            records={},
        )

        class MissingOrigin:
            def read_json(self, digest):
                raise ArtifactIntegrityError("SYNTHETIC missing original")

        refused = False
        try:
            verify_cards(context, MissingOrigin(), [card])
        except ArtifactIntegrityError:
            refused = True
        checks = {
            "hash_alone_without_original_is_rejected": refused,
            "rule_contains_no_official_fact": "记忆本身不是证据" in POLICIES[policy_id],
        }
    if not all(checks.values()):
        raise ValueError("installed_memory_policy_check_failed")
    return {
        "policy_id": policy_id,
        "checks": checks,
        "classification": "bounded_operating_policy_checks_not_domain_semantic_proof",
        "implementation_sha256": content_hash(
            {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [
                    Path("memory/policies.py"),
                    Path("control/contracts.py"),
                    Path("investigation/evidence.py"),
                ]
            }
        ),
    }

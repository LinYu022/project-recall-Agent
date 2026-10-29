from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text
from ingestion.contracts import Digest
from investigation.contracts import FixedConfig, InvestigationRequest
from retrieval.contracts import SearchScope

Method = Literal["fixed", "single_agent", "parallel_tools", "multi_agent"]
Family = Literal["main", "retrieval", "context", "memory_frozen", "memory_sequential"]
Memory = Literal["none", "semantic", "semantic_episode", "all"]


class Case(Contract):
    case_id: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]{0,59}$")]
    event_cluster_id: Text
    split: Literal["development", "test"]
    category: Literal[
        "direct",
        "cross_source",
        "counterevidence",
        "wrong_relation",
        "insufficient",
        "missing_conditions",
        "time_boundary",
        "scope_exclusion",
    ]
    sources: Annotated[list[Text], Field(min_length=1, max_length=10)]
    scope: SearchScope
    request: InvestigationRequest
    reference_sha256: Digest


class Arm(Contract):
    arm_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
    family: Family
    method: Method
    cases: Annotated[list[Text], Field(min_length=1)]
    retrieval: Literal["bm25", "dense", "hybrid", "hybrid_rerank"] = "hybrid"
    context: Literal["full_history", "recent_window", "structured"] = "structured"
    memory: Memory = "none"
    repeats: Annotated[int, Field(strict=True, ge=1, le=5)] = 1


class Protocol(Contract):
    schema_version: Literal["evaluation-protocol/0.1"] = "evaluation-protocol/0.1"
    dataset_release_id: Digest
    manifest_sha256: Digest
    cases: Annotated[list[Case], Field(min_length=1)]
    arms: Annotated[list[Arm], Field(min_length=1)]
    profile: FixedConfig
    model_id: Literal["deepseek-flash"] = "deepseek-flash"
    endpoint: Literal["https://api.deepseek.com"] = "https://api.deepseek.com"
    previously_seen_campaigns: list[Text]
    cluster_campaigns: dict[str, list[Text]]
    frozen_prefix_sha256: Digest
    label_author: Literal["implementation_agent"] = "implementation_agent"
    quality_targets: dict
    exclusions: Literal["none_after_freeze_all_failures_retained"] = (
        "none_after_freeze_all_failures_retained"
    )
    random_seed: Literal[20261028] = 20261028
    bootstrap_draws: Literal[2000] = 2000
    minimum_clusters_for_interval: Literal[5] = 5

    @model_validator(mode="after")
    def design(self):
        by_id = {c.case_id: c for c in self.cases}
        if len(by_id) != len(self.cases) or len({a.arm_id for a in self.arms}) != len(self.arms):
            raise ValueError("duplicate_case_or_arm")
        memberships = {}
        for group, campaigns in self.cluster_campaigns.items():
            for campaign in campaigns:
                if campaign in memberships:
                    raise ValueError("campaign_in_multiple_event_clusters")
                memberships[campaign] = group
        split_by_group: dict[str, str] = {}
        for case in self.cases:
            if case.event_cluster_id not in self.cluster_campaigns:
                raise ValueError("case_event_cluster_unknown")
            if split_by_group.setdefault(case.event_cluster_id, case.split) != case.split:
                raise ValueError("event_cluster_leaks_between_splits")
            if case.split == "test" and set(self.cluster_campaigns[case.event_cluster_id]) & set(
                self.previously_seen_campaigns
            ):
                raise ValueError("previously_seen_event_cannot_be_a_test")
            if len(case.sources) != len(set(case.sources)) or case.request.task_id != case.case_id:
                raise ValueError("case_source_or_task_identity_invalid")
            if (
                case.scope.campaign_number
                and case.scope.campaign_number not in self.cluster_campaigns[case.event_cluster_id]
            ):
                raise ValueError("case_campaign_outside_declared_cluster")
            if case.request.expected_release_id not in {None, self.dataset_release_id}:
                raise ValueError("case_request_release_differs")
            if any(
                s.rsplit("-", 1)[-1] in memberships
                and memberships[s.rsplit("-", 1)[-1]] != case.event_cluster_id
                for s in case.sources
            ):
                raise ValueError("paired_sources_cross_statistical_clusters")
        main = [a for a in self.arms if a.family == "main"]
        if {a.method for a in main} != {
            "fixed",
            "single_agent",
            "parallel_tools",
            "multi_agent",
        } or len(main) != 4:
            raise ValueError("four_main_methods_required")
        if len({tuple(a.cases) for a in main}) != 1 or len({a.repeats for a in main}) != 1:
            raise ValueError("main_methods_need_the_same_denominator_and_repeats")
        for arm in self.arms:
            if len(set(arm.cases)) != len(arm.cases) or not set(arm.cases) <= by_id.keys():
                raise ValueError("arm_uses_undefined_or_duplicate_cases")
            if arm.family == "main" and (
                arm.retrieval != self.profile.retrieval_method
                or arm.context != self.profile.context_mode
                or arm.memory != "none"
            ):
                raise ValueError("main_comparison_has_unequal_configuration")
            if arm.family == "retrieval" and (
                arm.method != "fixed"
                or arm.context != self.profile.context_mode
                or arm.memory != "none"
            ):
                raise ValueError("retrieval_ablation_changes_other_factors")
            if arm.family == "context" and (
                arm.method != "single_agent"
                or arm.retrieval != self.profile.retrieval_method
                or arm.memory != "none"
            ):
                raise ValueError("context_ablation_changes_other_factors")
            if arm.family.startswith("memory") and (
                arm.method != "single_agent"
                or arm.retrieval != self.profile.retrieval_method
                or arm.context != self.profile.context_mode
            ):
                raise ValueError("memory_ablation_changes_other_factors")
        for family in ["retrieval", "context", "memory_frozen", "memory_sequential"]:
            selected = [a for a in self.arms if a.family == family]
            if (
                len({tuple(a.cases) for a in selected}) > 1
                or len({a.repeats for a in selected}) > 1
            ):
                raise ValueError("ablation_methods_need_identical_tasks_and_repeats")
        if self.profile.max_model_calls != 8 or self.profile.total_generation_tokens != 4000:
            raise ValueError("common_frozen_model_envelope_changed")
        return self


def cells(protocol):
    return [
        {
            "cell_id": f"{arm.arm_id}:{case_id}:{repeat}",
            "arm_id": arm.arm_id,
            "case_id": case_id,
            "repeat": repeat,
            "family": arm.family,
            "method": arm.method,
            "event_cluster_id": next(
                c.event_cluster_id for c in protocol.cases if c.case_id == case_id
            ),
        }
        for arm in protocol.arms
        for repeat in range(1, arm.repeats + 1)
        for case_id in arm.cases
    ]


def request_ceiling(protocol):
    by_id = {c.case_id: c for c in protocol.cases}
    maximum = 0
    for cell in cells(protocol):
        case = by_id[cell["case_id"]]
        # These are preset server guards, never an estimate based on generated outcomes.
        guarded = case.category in {
            "missing_conditions",
            "time_boundary",
            "scope_exclusion",
            "insufficient",
        }
        maximum += (
            0 if guarded else 1 if cell["method"] == "fixed" else protocol.profile.max_model_calls
        )
    return maximum

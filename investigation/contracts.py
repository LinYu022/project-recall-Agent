from typing import Annotated, Literal

from pydantic import Field, model_validator

from domain.tasks import Contract, Text, TimeScope
from entities.scopes import RecordLocator
from ingestion.contracts import Digest
from retrieval.contracts import SearchScope

Disposition = Literal[
    "answered",
    "needs_clarification",
    "insufficient_evidence",
    "no_matching_records",
    "unsupported_time",
]
ClaimKind = Literal[
    "source_fact",
    "count",
    "relationship",
    "scope_exclusion",
    "insufficiency",
    "individual_applicability",
    "causal",
]
Alias = Annotated[str, Field(pattern=r"^E\d{3}$")]


class Pair(Contract):
    left: RecordLocator
    right: RecordLocator


class InvestigationRequest(Contract):
    task_id: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]+$")]
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    retrieval_query: Annotated[str, Field(min_length=1, max_length=1000)]
    intent: Literal[
        "record_lookup", "cross_source", "complaint_statistics", "applicability", "relationship"
    ]
    expected_release_id: Digest | None = None
    expected_time: TimeScope | None = None
    required_conditions: list[
        Literal["make", "model", "model_year", "production_date", "configurations"]
    ] = []
    components: list[Text] = []
    pairs: Annotated[list[Pair], Field(max_length=1)] = []


class FixedConfig(Contract):
    version: Literal["fixed-investigation/0.1"] = "fixed-investigation/0.1"
    retrieval_method: Literal["bm25", "dense", "hybrid", "hybrid_rerank"] = "hybrid"
    retrieve_k: Annotated[int, Field(strict=True, ge=1, le=10)] = 6
    expand_k: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    max_tool_calls: Annotated[int, Field(strict=True, ge=1, le=10)] = 6
    max_model_calls: Annotated[int, Field(strict=True, ge=1, le=8)] = 1
    prompt_characters: Annotated[int, Field(strict=True, ge=5000, le=50000)] = 24000
    generation_tokens: Annotated[int, Field(strict=True, ge=256, le=4096)] = 2000
    total_prompt_characters: Annotated[int, Field(strict=True, ge=5000, le=200000)] = 96000
    total_generation_tokens: Annotated[int, Field(strict=True, ge=256, le=16000)] = 4000
    wall_seconds: Annotated[int, Field(strict=True, ge=1, le=1800)] = 900
    repeated_action_limit: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    no_progress_limit: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    tool_timeout_retries: Annotated[int, Field(strict=True, ge=0, le=1)] = 1
    # Historical explicit configs retain their behavior. New service/agent defaults opt in.
    context_mode: Literal["full_history", "recent_window", "structured"] = "full_history"
    multi_agent_enabled: Annotated[bool, Field(strict=True)] = True
    branch_timeout_seconds: Annotated[float, Field(gt=0, le=900)] = 120.0


class ProposedClaim(Contract):
    claim_id: Annotated[str, Field(pattern=r"^C\d{3}$")]
    kind: ClaimKind
    statement: Annotated[str, Field(min_length=1, max_length=1200)]
    scope: SearchScope
    support_ids: Annotated[list[Alias], Field(min_length=1)]
    counter_ids: list[Alias] = []


class AnswerProposal(Contract):
    disposition: Disposition
    claims: Annotated[list[ProposedClaim], Field(max_length=8)]
    unresolved_fields: list[
        Literal[
            "make",
            "model",
            "model_year",
            "production_date",
            "configurations",
            "individual_vehicle",
            "causality",
            "source_coverage",
        ]
    ] = []

    @model_validator(mode="after")
    def distinct_claims(self):
        identifiers = [c.claim_id for c in self.claims]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate claim IDs")
        if self.disposition == "answered" and not self.claims:
            raise ValueError("answered requires explicit atomic claims")
        return self


class EvidenceCard(Contract):
    schema_version: Literal["evidence-card/0.1"] = "evidence-card/0.1"
    evidence_id: Digest
    evidence_key: Digest
    task_id: Text
    release_id: Digest
    kind: Literal["text", "structured_lookup", "count", "relationship"]
    origin_sha256: Digest
    selector: dict
    source_ids: list[Text]
    source_version_ids: list[Digest]
    scope_channel: Literal["matched", "pending", "excluded", "inspection"]
    payload: dict
    required_context_ids: list[Digest]


class EvidenceLocator(Contract):
    source_version_id: Digest
    unit_id: Digest
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, gt=0)]
    quote_sha256: Digest

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end <= self.start:
            raise ValueError("evidence locator end must exceed start")
        return self


class EvidenceSlot(Contract):
    slot_id: Text
    kind: Literal["text", "count", "relationship", "structured_lookup"]
    alternatives: list[EvidenceLocator] = []
    expected_fields: dict = Field(default_factory=dict)


class ReferenceClaim(Contract):
    reference_claim_id: Text
    statement: Text
    evidence_sets: Annotated[list[Annotated[list[Text], Field(min_length=1)]], Field(min_length=1)]
    forbidden_inferences: list[Text] = []


class ReferenceCase(Contract):
    case_id: Text
    split: Literal["exploration", "development", "test"]
    slots: list[EvidenceSlot]
    required_claims: list[ReferenceClaim]
    acceptable_dispositions: Annotated[list[Disposition], Field(min_length=1)]

    @model_validator(mode="after")
    def declared_slots(self):
        slots = {s.slot_id for s in self.slots}
        if len(slots) != len(self.slots):
            raise ValueError("duplicate reference slots")
        identifiers = [c.reference_claim_id for c in self.required_claims]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate reference claim IDs")
        if any(not set(group) <= slots for c in self.required_claims for group in c.evidence_sets):
            raise ValueError("reference claim uses undefined evidence slots")
        return self

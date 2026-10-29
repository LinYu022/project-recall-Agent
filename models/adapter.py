"""Minimal model contract, independent of HTTP and provider SDKs."""

from typing import Literal, Protocol

from pydantic import Field, StrictBool

from domain.tasks import Contract, Text

ModelScenario = Literal["success", "model-error", "bad-citation"]


class ModelIdentity(Contract):
    provider: Text
    model_id: Text
    is_simulated: StrictBool
    parameters: dict[str, Text] = Field(default_factory=dict)


class SourceSnippet(Contract):
    source_id: Text
    record_id: Text
    text: Text


class ModelInput(Contract):
    task_id: Text
    release_id: Text
    question: Text
    records: list[SourceSnippet]


class ModelOutput(Contract):
    text: Text
    cited_source_ids: list[Text]


class ModelAdapter(Protocol):
    identity: ModelIdentity

    def generate(self, request: ModelInput) -> dict: ...


class FakeModelAdapter:
    """A deterministic test double, not an LLM or a quality baseline."""

    def __init__(self, scenario: ModelScenario = "success"):
        self.scenario = scenario
        self.identity = ModelIdentity(
            provider="fake",
            model_id="fake-summary-v1",
            is_simulated=True,
            parameters={"scenario": scenario},
        )

    def generate(self, request: ModelInput) -> dict:
        if self.scenario == "model-error":
            raise RuntimeError("injected model failure")
        source_ids = sorted({record.source_id for record in request.records})
        if self.scenario == "bad-citation":
            source_ids = ["UNSEEN-SOURCE"]
        return {
            "text": f"Synthetic fixture contains {len(request.records)} record(s). No real vehicle conclusion is supported.",
            "cited_source_ids": source_ids,
        }

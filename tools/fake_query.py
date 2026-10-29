"""Synthetic records for execution tests, never NHTSA evidence."""

from typing import Literal, Protocol

from domain.tasks import Contract, TaskSpec, Text, VehicleScope
from runtime.config import DEMO_RELEASE, DEMO_SOURCE

Scenario = Literal["success", "empty", "missing-source", "tool-error"]
FIXTURE_SCOPE = VehicleScope(
    country="US",
    make="EXAMPLE",
    model="LEARNING-CAR",
    model_year=2020,
    component_topic="braking",
    campaign_number="00V000000",
)


class DemoRecord(Contract):
    source_id: Literal["SIM-COMPLAINT-001"]
    record_id: Text
    summary: Text


class QueryResult(Contract):
    data_kind: Literal["synthetic"]
    release_id: Literal["demo-fixture-v1"]
    records: list[DemoRecord]


class QueryTool(Protocol):
    def query(self, task: TaskSpec) -> dict: ...


class FakeQueryTool:
    def __init__(self, scenario: Scenario = "success"):
        self.scenario = scenario

    def query(self, task: TaskSpec) -> dict:
        if self.scenario == "tool-error":
            raise RuntimeError("injected tool failure")
        records = []
        if task.scope == FIXTURE_SCOPE and self.scenario != "empty":
            record = {
                "source_id": DEMO_SOURCE,
                "record_id": "SIM-RECORD-001",
                "summary": "SYNTHETIC training report of an intermittent braking response.",
            }
            if self.scenario == "missing-source":
                del record["source_id"]
            records.append(record)
        return {"data_kind": "synthetic", "release_id": DEMO_RELEASE, "records": records}

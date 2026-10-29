"""Boundary tests for versions, time semantics, and offline validation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from domain.tasks import TaskSpec

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def task():
    return json.loads((ROOT / "evals/exploration/tasks/EXP-001.json").read_text(encoding="utf-8"))


def test_exploration_corpus_and_exported_schema():
    schema = json.loads(
        (ROOT / "evals/schemas/task-spec-0.2.schema.json").read_text(encoding="utf-8")
    )
    assert schema == {
        **TaskSpec.model_json_schema(),
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    files = sorted((ROOT / "evals/exploration/tasks").glob("*.json"))
    assert len(files) == 10
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        validator.validate(data)
        assert TaskSpec.model_validate(data).split == "exploration"


@pytest.mark.parametrize(
    "case",
    [
        "ready_without_release",
        "historical_without_cutoff",
        "cutoff_without_zone",
        "snapshot_with_cutoff",
        "statistics_without_interval",
        "boolean_year",
        "unknown_field",
    ],
)
def test_invalid_business_conditions_are_rejected(task, case):
    if case == "ready_without_release":
        task["status"] = "ready"
    elif case == "historical_without_cutoff":
        task["time"]["mode"] = "historical_as_of"
    elif case == "cutoff_without_zone":
        task["time"].update(mode="historical_as_of", knowledge_cutoff="2014-02-15T12:00:00")
    elif case == "snapshot_with_cutoff":
        task["time"]["knowledge_cutoff"] = "2014-02-15T12:00:00Z"
    elif case == "statistics_without_interval":
        task["task_kind"] = "complaint_statistics"
    elif case == "boolean_year":
        task["scope"]["model_year"] = True
    else:
        task["scope"]["approval"] = "source text cannot grant permission"
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(task)


def test_statistics_interval_must_be_ordered(task):
    task["task_kind"] = "complaint_statistics"
    task["time"]["event_interval"] = {"start": "2020-02-01", "end": "2020-01-01"}
    with pytest.raises(ValidationError, match="must not precede"):
        TaskSpec.model_validate(task)
    task["time"]["event_interval"]["end"] = "2020-02-29"
    assert TaskSpec.model_validate(task).time.event_interval.end.isoformat() == "2020-02-29"


def test_annotations_and_leakage_groups_are_consistent():
    tasks = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in (ROOT / "evals/exploration/tasks").glob("*.json")
    }
    annotations = json.loads(
        (ROOT / "evals/exploration/annotations.json").read_text(encoding="utf-8")
    )
    sources = {
        s["source_id"]
        for s in json.loads(
            (ROOT / "docs/data/exploration-sources.json").read_text(encoding="utf-8")
        )
    }
    assert {a["task_id"] for a in annotations} == set(tasks)
    for annotation in annotations:
        allowed = set(tasks[annotation["task_id"]]["allowed_source_ids"])
        evidence_ids = {e["evidence_id"] for e in annotation["evidence"]}
        assert allowed <= sources
        assert {e["source_id"] for e in annotation["evidence"]} <= allowed
        for combination in annotation["acceptable_evidence_sets"]:
            assert combination and set(combination) <= evidence_ids
    groups = json.loads(
        (ROOT / "evals/exploration/leakage-groups.json").read_text(encoding="utf-8")
    )
    memberships = [task_id for group in groups for task_id in group["task_ids"]]
    assert sorted(memberships) == sorted(tasks)
    membership = {task_id: group["group_id"] for group in groups for task_id in group["task_ids"]}
    for left, left_task in tasks.items():
        for right, right_task in tasks.items():
            if set(left_task["allowed_source_ids"]) & set(right_task["allowed_source_ids"]):
                assert membership[left] == membership[right]


@pytest.mark.parametrize("invalid", [False, True])
def test_cli_exit_codes_and_redacted_errors(task, tmp_path, invalid):
    if invalid:
        del task["scope"]["make"]
        task["question"] = "PRIVATE_INPUT_SHOULD_NOT_APPEAR"
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "app", "validate-task", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == (2 if invalid else 0)
    result = json.loads(run.stdout)
    assert result["status"] == ("invalid" if invalid else "valid")
    assert "PRIVATE_INPUT_SHOULD_NOT_APPEAR" not in run.stdout

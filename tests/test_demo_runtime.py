"""Execution boundaries, source validation, and artifact integrity."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from domain.tasks import TaskSpec
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from runtime.config import DevSettings, load_settings
from runtime.demo import run_demo
from tools.fake_query import FakeQueryTool

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo_task():
    return TaskSpec.model_validate_json(
        (ROOT / "examples/demo-task.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_ARTIFACT_DIR", str(tmp_path / "runs"))
    return load_settings(ROOT / "examples/dev-readonly.toml")


@pytest.mark.parametrize(
    "scenario,status,count,error",
    [
        ("success", "succeeded", 1, None),
        ("empty", "succeeded", 0, None),
        ("missing-source", "failed", None, "tool_output_invalid"),
        ("tool-error", "failed", None, "tool_error"),
    ],
)
def test_results_and_audit_trail(demo_task, settings, scenario, status, count, error):
    result = run_demo(
        demo_task, settings, {"fake_query": FakeQueryTool(scenario)}, scenario=scenario
    )
    assert (result["status"], result["record_count"], result["error_code"]) == (
        status,
        count,
        error,
    )
    assert result["calls_used"] == 1
    run_dir = Path(result["run_dir"])
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")) == result
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [e["sequence"] for e in events] == [1, 2, 3, 4]
    assert events[1]["event_type"] == "call_started"
    for event in events:
        for key in ("run_id", "task_id", "call_id", "release_id", "grant_id", "revision_id"):
            assert event[key] == result[key]
    store = ArtifactStore(run_dir)
    assert (
        store.read_json(result["input_artifact"]["sha256"])["task"]["task_id"] == demo_task.task_id
    )
    if scenario == "tool-error":
        assert result["output_artifact"] is None
    else:
        raw = store.read_json(result["output_artifact"]["sha256"])
        assert raw["data_kind"] == "synthetic"
        if scenario == "missing-source":
            assert "source_id" not in raw["records"][0]


@pytest.mark.parametrize(
    "case,reason",
    [
        ("tool", "tool_not_allowed"),
        ("budget", "tool_call_limit_reached"),
        ("grant", "context_not_granted"),
        ("release", "context_not_granted"),
        ("revision", "context_not_granted"),
        ("source", "source_not_granted"),
        ("scope", "scope_not_granted"),
    ],
)
def test_guards_prevent_tool_invocation(demo_task, settings, case, reason):
    class NeverCall:
        def query(self, task):
            pytest.fail("denied tool must not execute")

    data, config = demo_task.model_dump(mode="json"), settings.model_dump(mode="json")
    name = "fake_query"
    if case == "tool":
        name = "not_allowed"
    elif case == "budget":
        config["max_tool_calls"] = 0
    elif case in ("grant", "release", "revision"):
        data[case + "_id"] = "different-context"
    elif case == "source":
        data["allowed_source_ids"] = ["UNGRANTED-SOURCE"]
    else:
        data["scope"]["make"] = "OTHER-MAKE"
    result = run_demo(
        TaskSpec.model_validate(data),
        DevSettings.model_validate(config),
        {"fake_query": NeverCall()},
        tool_name=name,
    )
    assert (result["status"], result["error_code"], result["calls_used"]) == ("denied", reason, 0)
    assert result["output_artifact"] is None
    events = (Path(result["run_dir"]) / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "call_started"' not in events


def test_corrupted_artifact_cannot_be_read_or_overwritten(tmp_path):
    store = ArtifactStore(tmp_path)
    value = {"data_kind": "synthetic", "source_id": "SIM-COMPLAINT-001"}
    reference = store.put_json(value)
    path = tmp_path / "artifacts" / (reference["sha256"] + ".json")
    path.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        store.read_json(reference["sha256"])
    with pytest.raises(ArtifactIntegrityError, match="existing artifact"):
        store.put_json(value)
    assert path.read_text(encoding="utf-8") == '{"tampered":true}'


def test_bad_digest_and_missing_artifact_do_not_create_directories(tmp_path):
    root = tmp_path / "not-created"
    store = ArtifactStore(root)
    with pytest.raises(ArtifactIntegrityError):
        store.read_json("../../outside")
    with pytest.raises(FileNotFoundError):
        store.read_json("0" * 64)
    assert not root.exists()


def test_raw_tool_exception_is_not_logged(demo_task, settings):
    class FailingTool:
        def query(self, task):
            raise RuntimeError("SENSITIVE_UPSTREAM_ERROR")

    result = run_demo(demo_task, settings, {"fake_query": FailingTool()})
    assert result["error_code"] == "tool_error"
    for path in Path(result["run_dir"]).rglob("*.json*"):
        assert "SENSITIVE_UPSTREAM_ERROR" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "scenario,code", [("success", 0), ("missing-source", 5), ("tool-error", 5)]
)
def test_cli_scenarios(settings, scenario, code):
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "app",
            "run-demo",
            "examples/demo-task.json",
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == code
    assert json.loads(run.stdout)["data_kind"] == "synthetic"

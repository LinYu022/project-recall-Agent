"""Offline checks for approved experiment caps and failure diagnostics; no real API calls."""

import io
import json

import pytest

from models.chat_api import ChatJSONModel, ModelCallFailed
from models.request_allowance import RequestAllowance
from scripts.verify_investigation_real import RecordedModel


def test_prior_requests_and_recreated_process_share_one_nonresetting_limit(tmp_path):
    path = tmp_path / "allowance.json"
    history = [
        {"sequence": 1, "case_id": "CASE", "method": "fixed", "status": "historical_recorded"}
    ]
    limits = {"CASE": {"fixed": 1, "single_agent": 2}}
    first = RequestAllowance(path, limit=3, case_limits=limits, historical=history)
    with pytest.raises(ModelCallFailed, match="approved_request_budget"):
        first.reserve("CASE", "fixed", "not-a-new-baseline")
    sequence = first.reserve("CASE", "single_agent", "first")
    first.finish(sequence, status="failed", error_code="synthetic_transport", usage=None)
    second = RequestAllowance(path, limit=3, case_limits=limits, historical=[])
    second.reserve("CASE", "single_agent", "second")  # Unknown in-flight requests stay charged.
    with pytest.raises(ModelCallFailed, match="approved_request_budget"):
        second.reserve("CASE", "single_agent", "third")
    assert second.snapshot()["remaining"] == 0
    assert second.snapshot()["entries"][1]["error_code"] == "synthetic_transport"


def test_allowance_never_adds_an_unapproved_case_or_changes_approved_limits(tmp_path):
    path = tmp_path / "allowance.json"
    allowance = RequestAllowance(path, limit=1, case_limits={"CASE": {"single_agent": 1}})
    with pytest.raises(ModelCallFailed, match="outside_approved"):
        allowance.reserve("OTHER", "single_agent", "unapproved")
    with pytest.raises(ValueError, match="allowance_changed"):
        RequestAllowance(path, limit=14, case_limits={"CASE": {"single_agent": 14}})
    assert allowance.snapshot()["requests_reserved"] == 0


def test_existing_lock_is_not_deleted_or_treated_as_a_reset(tmp_path):
    path = tmp_path / "allowance.json"
    lock = path.with_suffix(".lock")
    lock.write_text("another experiment writer")
    with pytest.raises(FileExistsError):
        RequestAllowance(path, limit=1, case_limits={})
    assert lock.read_text() == "another experiment writer"


def test_recorded_error_replays_without_another_call_or_allowance_reservation(tmp_path):
    class InvalidModel:
        identity = {"model_id": "synthetic-failure", "is_simulated": True}
        calls = 0

        def generate(self, *args, **kwargs):
            self.calls += 1
            raise ModelCallFailed(
                "model_content_invalid_json",
                {
                    "raw_content": "{",
                    "finish_reason": "length",
                    "usage": {"completion_tokens": 20, "total_tokens": 30},
                },
            )

    delegate = InvalidModel()
    limits = {"CASE": {"single_agent": 1}}
    allowance = RequestAllowance(tmp_path / "requests.json", limit=1, case_limits=limits)
    model = RecordedModel(delegate, tmp_path / "cache", live=True, allowance=allowance)
    model.current_case, model.current_method = "CASE", "single_agent"
    with pytest.raises(ModelCallFailed):
        model.generate("json", {}, max_tokens=20)
    replay = RecordedModel(delegate, tmp_path / "cache", replay=True, allowance=allowance)
    with pytest.raises(ModelCallFailed) as caught:
        replay.generate("json", {}, max_tokens=20)
    assert caught.value.diagnostics["replayed"] is True
    assert delegate.calls == 1 and replay.calls == 0 and replay.replays == 1
    assert allowance.snapshot()["requests_reserved"] == 1


def test_invalid_json_retains_usage_and_content_without_serializing_credential():
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [{"message": {"content": "{"}, "finish_reason": "length"}],
                        "usage": {"completion_tokens": 2000, "total_tokens": 2500},
                    }
                ).encode()
            )

    model = ChatJSONModel({"DEEPSEEK_API_KEY": "synthetic-secret"}, opener=Opener())
    with pytest.raises(ModelCallFailed, match="content_invalid_json") as caught:
        model.generate("json", {})
    diagnostics = caught.value.diagnostics
    assert diagnostics["usage"]["completion_tokens"] == 2000 and diagnostics["raw_content"] == "{"
    assert diagnostics["finish_reason"] == "length"
    assert "synthetic-secret" not in json.dumps(diagnostics)


def test_unknown_old_usage_conservatively_reduces_remaining_case_completion_budget(tmp_path):
    history = [
        {
            "sequence": 1,
            "case_id": "CASE",
            "method": "single_agent",
            "status": "historical_recorded",
            "usage": None,
            "max_tokens": 2000,
        },
        {
            "sequence": 2,
            "case_id": "CASE",
            "method": "single_agent",
            "status": "completed",
            "usage": {"completion_tokens": 235},
            "max_tokens": 2000,
        },
    ]
    allowance = RequestAllowance(
        tmp_path / "requests.json",
        limit=14,
        case_limits={"CASE": {"single_agent": 6}},
        historical=history,
    )
    assert allowance.completion_remaining("CASE", "single_agent") == 1765
    with pytest.raises(ModelCallFailed, match="approved_request_budget"):
        allowance.reserve("CASE", "single_agent", "too-large", max_tokens=2000)
    allowance.reserve("CASE", "single_agent", "bounded", max_tokens=1765)
    assert allowance.completion_remaining("CASE", "single_agent") == 0

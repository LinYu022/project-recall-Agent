"""Controlled temporal fixtures are not historical NHTSA archives."""

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from ingestion.__main__ import main
from ingestion.temporal import TemporalRequest, select_versions
from tests.source_fixtures import capture_rows, complaint, recall


def observed(root, rows, when, kind="complaint_api"):
    snapshot = capture_rows(root, rows, kind)
    stamp = datetime.fromisoformat(when)
    values = {"request_started_at": stamp, "completed_at": stamp, "fetched_at": stamp}
    snapshot = snapshot.model_copy(update=values)
    (root / "snapshots" / f"{snapshot.snapshot_id}.json").write_text(
        snapshot.model_dump_json(), encoding="utf-8"
    )
    return snapshot


def request(snapshots, mode="historical_as_of", cutoff="2020-03-01T00:00:00Z", interval=None):
    return TemporalRequest.model_validate(
        {
            "snapshot_ids": [s.snapshot_id for s in snapshots],
            "time": {
                "mode": mode,
                "event_interval": interval,
                "knowledge_cutoff": cutoff,
                "date_filter_basis": "Controlled mechanism test only",
            },
        }
    )


def test_late_record_with_old_event_is_excluded_before_observation(tmp_path):
    snapshot = observed(
        tmp_path,
        [complaint(event="01/01/2010") | {"dateComplaintFiled": "04/01/2020"}],
        "2020-04-01T00:00:00+00:00",
    )
    result = select_versions(
        tmp_path, request([snapshot], interval={"start": "2010-01-01", "end": "2010-12-31"})
    )
    assert result["status"] == "unsupported" and result["selected_count"] == 0
    assert result["exclusion_reasons"] == {"version_not_observed_by_cutoff": 1}
    current = select_versions(
        tmp_path,
        request(
            [snapshot], "snapshot_analysis", None, {"start": "2010-01-01", "end": "2010-12-31"}
        ),
    )
    assert current["selected_count"] == 1 and current["analysis_label"] == "fixed_snapshot_analysis"


def test_revision_after_cutoff_cannot_replace_old_version(tmp_path):
    old = observed(tmp_path, [complaint(summary="SYNTHETIC ORIGINAL")], "2020-02-01T00:00:00+00:00")
    new = observed(tmp_path, [complaint(summary="SYNTHETIC REVISION")], "2020-04-01T00:00:00+00:00")
    result = select_versions(tmp_path, request([old, new]))
    assert (
        result["status"] == "partial" and result["selected_count"] == result["excluded_count"] == 1
    )
    assert result["selected"][0]["references"][0]["snapshot_id"] == old.snapshot_id
    assert result["selected"][0]["references"][0]["observed_at"] == old.fetched_at.isoformat()


def test_two_visible_versions_need_explicit_resolution(tmp_path):
    old = observed(tmp_path, [complaint(summary="SYNTHETIC ONE")], "2020-01-01T00:00:00+00:00")
    new = observed(tmp_path, [complaint(summary="SYNTHETIC TWO")], "2020-02-01T00:00:00+00:00")
    result = select_versions(tmp_path, request([old, new]))
    assert result["status"] == "unsupported"
    assert result["exclusion_reasons"] == {"conflicting_visible_versions": 2}
    with pytest.raises(ValueError, match="one body per source"):
        select_versions(tmp_path, request([old, new], "snapshot_analysis", None))


def test_repeated_same_bytes_preserve_one_version_and_only_eligible_references(tmp_path):
    old = observed(tmp_path, [complaint()], "2020-02-01T00:00:00+00:00")
    new = observed(tmp_path, [complaint()], "2020-04-01T00:00:00+00:00")
    result = select_versions(tmp_path, request([old, new]))
    assert result["selected_count"] == 1 and result["status"] == "selected"
    assert len(result["selected"][0]["references"]) == 1
    assert result["selected"][0]["first_observed_in_input_set"] == old.fetched_at.isoformat()


@pytest.mark.parametrize(
    "event,reason",
    [
        ("impossible", "event_date_unavailable"),
        (None, "event_date_unavailable"),
        ("01/01/2019", "event_outside_interval"),
    ],
)
def test_event_filter_never_substitutes_filed_date(tmp_path, event, reason):
    snapshot = observed(tmp_path, [complaint(event=event)], "2020-02-01T00:00:00+00:00")
    result = select_versions(
        tmp_path, request([snapshot], interval={"start": "2020-01-01", "end": "2020-12-31"})
    )
    assert result["selected_count"] == 0 and result["exclusion_reasons"] == {reason: 1}
    assert result["status"] == ("empty" if reason == "event_outside_interval" else "unsupported")


def test_event_interval_boundaries_are_inclusive_and_not_partial_for_normal_filtering(tmp_path):
    snapshot = observed(
        tmp_path,
        [
            complaint(event="01/01/2020"),
            complaint(12345679, event="12/31/2020"),
            complaint(12345680, event="01/01/2019"),
        ],
        "2020-02-01T00:00:00+00:00",
    )
    result = select_versions(
        tmp_path,
        request(
            [snapshot], "snapshot_analysis", None, {"start": "2020-01-01", "end": "2020-12-31"}
        ),
    )
    assert result["selected_count"] == 2 and result["status"] == "selected"


def test_old_recall_date_is_not_version_publication_proof(tmp_path):
    snapshot = observed(tmp_path, [recall()], "2026-01-01T00:00:00+00:00", "recall_api")
    result = select_versions(tmp_path, request([snapshot]))
    assert result["status"] == "unsupported"


def test_empty_response_after_cutoff_cannot_prove_historical_zero(tmp_path):
    snapshot = observed(tmp_path, [], "2020-04-01T00:00:00+00:00")
    assert select_versions(tmp_path, request([snapshot]))["status"] == "unsupported"
    assert (
        select_versions(tmp_path, request([snapshot], "snapshot_analysis", None))["status"]
        == "empty"
    )


def test_cutoff_requires_timezone_and_exact_observation_boundary_is_allowed(tmp_path):
    snapshot = observed(tmp_path, [complaint()], "2020-03-01T00:00:00+00:00")
    assert select_versions(tmp_path, request([snapshot]))["selected_count"] == 1
    with pytest.raises(ValidationError):
        request([snapshot], cutoff="2020-03-01T00:00:00")
    with pytest.raises(ValidationError):
        request([snapshot, snapshot])


def test_frozen_selection_does_not_change_when_another_snapshot_is_added(tmp_path):
    old = observed(tmp_path, [complaint()], "2020-02-01T00:00:00+00:00")
    selected = request([old], "snapshot_analysis", None)
    before = select_versions(tmp_path, selected)
    observed(tmp_path, [complaint(summary="SYNTHETIC LATER")], "2020-04-01T00:00:00+00:00")
    assert select_versions(tmp_path, selected) == before


def test_cli_refusal_saves_reviewable_selection_and_does_not_overwrite(
    tmp_path, monkeypatch, capsys
):
    snapshot = observed(tmp_path, [complaint()], "2020-04-01T00:00:00+00:00")
    path = tmp_path / "request.json"
    path.write_text(request([snapshot]).model_dump_json(), encoding="utf-8")
    output = tmp_path / "selection.json"
    monkeypatch.setattr(
        "sys.argv",
        ["ingestion", "select", str(path), "--root", str(tmp_path), "--output", str(output)],
    )
    assert main() == 5
    assert json.loads(capsys.readouterr().out)["status"] == "unsupported"
    saved = output.read_bytes()
    assert main() == 2
    assert output.read_bytes() == saved
    capsys.readouterr()

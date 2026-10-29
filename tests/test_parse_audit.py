"""Batch failure ledgers and negation/condition preservation use synthetic inputs only."""

import json

import pytest
from pydantic import ValidationError

from parsing.__main__ import main
from parsing.audit import AuditManifest, audit_sources, coverage_summary
from parsing.chunk_contracts import ChunkConfig, StructureRules, load_chunk_config
from parsing.chunks import build_chunks, checked_chunks, cite_chunk, expand_chunk
from parsing.citations import resolve_citation
from parsing.contracts import Citation, ParseConfig, ParseRequest
from parsing.parser import parse_source, text_hash
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.source_fixtures import capture_rows, complaint, recall
from tests.test_chunks import profiles, saved_parse
from tests.test_parsing import capture_pdf, pdf_bytes


def audit(tmp_path, requests, *, rules=None, config=None):
    return audit_sources(
        tmp_path / "raw",
        tmp_path / "parsed",
        tmp_path / "chunks",
        AuditManifest(requests=requests, parse_config=config or ParseConfig()),
        [load_chunk_config(s) for s in ("structure", "fixed")],
        rules or StructureRules(rules_version="SYNTHETIC-no-layouts", documents=[]),
    )


def request(snapshot, ordinal=0):
    return ParseRequest(snapshot_id=snapshot.snapshot_id, ordinal=ordinal)


def test_batch_records_page_failures_and_continues_to_good_record(tmp_path):
    raw = tmp_path / "raw"
    empty = capture_pdf(raw, pdf_bytes([None]))
    partial = capture_pdf(raw, pdf_bytes(["SYNTHETIC success", None]))
    encrypted = capture_pdf(raw, pdf_bytes(["SYNTHETIC"], encrypted=True))
    damaged = capture_pdf(raw, b"%PDF-SYNTHETIC BROKEN")
    good = capture_rows(raw, [complaint(summary="SYNTHETIC not a confirmed defect")])
    result = audit(tmp_path, [request(s) for s in (empty, partial, encrypted, damaged, good)])
    assert [i["parse_status"] for i in result["items"]] == [
        "needs_review_or_ocr",
        "partial",
        "failed",
        "failed",
        "parsed",
    ]
    assert result["counts"] == dict(
        requests=5, parsed=1, chunk_sets=2, blocked_items=4, failures=4, warnings=0
    )
    assert result["failures"][1]["page_number"] == 2
    assert result["failures"][1]["parse_artifact_sha256"]
    assert all(
        a["status"] == "skipped_incomplete_parse"
        for i in result["items"][:4]
        for a in i["chunk_attempts"]
    )
    assert all(f["next_action"] for f in result["failures"])
    assert result["ocr_performed"] is False and result["semantic_support"] == "not_evaluated"
    assert "not a confirmed defect" not in json.dumps(result)
    assert (
        audit(tmp_path, [request(s) for s in (empty, partial, encrypted, damaged, good)]) == result
    )


def test_unreviewed_pdf_is_blocked_for_structure_and_labeled_for_fixed(tmp_path):
    snapshot = capture_pdf(tmp_path / "raw", pdf_bytes(["SYNTHETIC unknown layout"]))
    result = audit(tmp_path, [request(snapshot)])
    assert result["failures"][0]["code"] == "pdf_layout_not_reviewed"
    assert result["failures"][0]["strategy"] == "structure"
    assert result["warnings"][0]["code"] == "layout_not_reviewed"
    assert result["items"][0]["chunk_attempts"][1]["status"] == "built"


def test_bad_hash_missing_snapshot_and_bad_row_do_not_prevent_good_item(tmp_path):
    raw = tmp_path / "raw"
    bad = capture_rows(raw, [complaint(summary="SYNTHETIC private raw narrative")])
    (raw / "objects" / bad.raw.sha256).write_bytes(b"SYNTHETIC TAMPERING")
    good = capture_rows(raw, [complaint(summary="SYNTHETIC good row")])
    result = audit(
        tmp_path,
        [request(bad), ParseRequest(snapshot_id="0" * 32), request(good, 99), request(good)],
    )
    assert [f["code"] for f in result["failures"]] == [
        "source_integrity_failed",
        "source_io_failed",
        "source_record_rejected",
    ]
    assert result["counts"]["chunk_sets"] == 2
    assert all(f["source"] is None for f in result["failures"])
    assert "private raw narrative" not in json.dumps(result)


@pytest.mark.parametrize(
    "config,code",
    [
        (ParseConfig(max_pages=1), "page_limit_or_empty_document"),
        (ParseConfig(max_text_characters=1), "text_limit_exceeded"),
    ],
)
def test_limits_are_in_ledger_not_silent_truncation(tmp_path, config, code):
    snapshot = capture_pdf(tmp_path / "raw", pdf_bytes(["SYNTHETIC first", "SYNTHETIC second"]))
    result = audit(tmp_path, [request(snapshot)], config=config)
    assert result["failures"][0]["code"] == code
    assert result["counts"]["chunk_sets"] == 0
    assert result["recipe"]["manifest"]["parse_config"] == config.model_dump(mode="json")


def test_root_only_text_is_visible_as_coverage_warning(tmp_path):
    parsed, _ = saved_parse(
        tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC body", "SYNTHETIC footer"]
    )
    rules = profiles(parsed).model_dump(mode="json")
    section = rules["documents"][0]["sections"][1]
    section["ranges"] = section["ranges"][:1]
    section["parts"][0]["ranges"] = section["parts"][0]["ranges"][:1]
    result = audit(
        tmp_path,
        [ParseRequest(**{key: parsed["source"][key] for key in ("snapshot_id", "ordinal")})],
        rules=StructureRules.model_validate(rules),
    )
    assert result["counts"]["failures"] == 0
    assert result["warnings"][0]["code"] == "root_only_text"
    gap = result["items"][0]["chunk_attempts"][0]["coverage"]["root_only_ranges"][0]
    assert gap["page_number"] == 3 and gap["nonwhitespace_characters"] == len("SYNTHETICfooter")
    assert result["items"][0]["chunk_attempts"][1]["coverage"]["root_only_ranges"] == []


def test_reviewed_ranges_with_changed_hash_are_ledgered_as_integrity_failure(tmp_path):
    parsed, _ = saved_parse(tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC body"])
    rules = profiles(parsed).model_dump(mode="json")
    rules["documents"][0]["sections"][0]["ranges"][0]["text_sha256"] = "0" * 64
    result = audit(
        tmp_path,
        [ParseRequest(snapshot_id=parsed["source"]["snapshot_id"])],
        rules=StructureRules.model_validate(rules),
    )
    assert [f["code"] for f in result["failures"]] == ["chunk_integrity_failed"] * 2


@pytest.mark.parametrize("requests", [[], [{"snapshot_id": "0" * 32}] * 2])
def test_invalid_manifest_is_rejected_before_writes(requests):
    with pytest.raises(ValidationError):
        AuditManifest.model_validate(dict(requests=requests))


def test_audit_requires_exactly_two_distinct_strategies(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        audit_sources(
            tmp_path,
            tmp_path,
            tmp_path,
            AuditManifest(requests=[ParseRequest(snapshot_id="0" * 32)]),
            [load_chunk_config("fixed")] * 2,
            StructureRules(rules_version="SYNTHETIC", documents=[]),
        )


def test_artifact_disk_error_aborts_instead_of_becoming_source_failure(tmp_path, monkeypatch):
    snapshot = capture_rows(tmp_path / "raw", [complaint()])

    def disk_full(*args):
        raise OSError("SYNTHETIC disk full")

    monkeypatch.setattr(ArtifactStore, "put_json", disk_full)
    with pytest.raises(OSError, match="disk full"):
        audit(tmp_path, [request(snapshot)])


def test_unexpected_programming_error_is_not_silenced(tmp_path, monkeypatch):
    def broken(*args):
        raise RuntimeError("SYNTHETIC bug")

    monkeypatch.setattr("parsing.audit.parse_source", broken)
    with pytest.raises(RuntimeError, match="bug"):
        audit(tmp_path, [ParseRequest(snapshot_id="0" * 32)])


@pytest.mark.parametrize("missing", [False, True])
def test_cli_saves_one_content_addressed_ledger_and_reports_exit_code(tmp_path, capsys, missing):
    snapshot = capture_rows(tmp_path / "raw", [complaint()])
    manifest = tmp_path / "request.json"
    manifest.write_text(
        AuditManifest(
            requests=[ParseRequest(snapshot_id="0" * 32) if missing else request(snapshot)]
        ).model_dump_json(),
        encoding="utf-8",
    )
    args = [
        "audit",
        str(manifest),
        "--raw-root",
        str(tmp_path / "raw"),
        "--parsed-root",
        str(tmp_path / "parsed"),
        "--chunk-root",
        str(tmp_path / "chunks"),
        "--audit-root",
        str(tmp_path / "audits"),
    ]
    assert main(args) == (4 if missing else 0)
    result = json.loads(capsys.readouterr().out)
    ledger = ArtifactStore(tmp_path / "audits").read_json(result["audit_artifact"]["sha256"])
    assert ledger["counts"]["failures"] == int(missing)
    assert main(args) == (4 if missing else 0)
    assert json.loads(capsys.readouterr().out)["audit_artifact"] == result["audit_artifact"]


@pytest.mark.parametrize("strategy", ["structure", "fixed"])
def test_cross_field_negation_and_condition_survive_expansion_and_citations(tmp_path, strategy):
    row = recall() | {
        "Summary": "SYNTHETIC a symptom was reported.",
        "Consequence": "SYNTHETIC reports do not establish a confirmed defect.",
        "Remedy": "SYNTHETIC only vehicles with package A; excludes package B.",
    }
    snapshot = capture_rows(tmp_path / "raw", [row], kind="recall_api")
    parsed = parse_source(tmp_path / "raw", request(snapshot))
    digest = ArtifactStore(tmp_path / "parsed").put_json(parsed)["sha256"]
    rules = StructureRules(rules_version="SYNTHETIC", documents=[])
    # Small windows deliberately separate words, retaining the exact parent and other fields.
    config = (
        load_chunk_config(strategy)
        if strategy == "structure"
        else ChunkConfig(
            config_version="SYNTHETIC-small", strategy="fixed", fixed_characters=17, fixed_overlap=3
        )
    )
    chunks = build_chunks(tmp_path / "raw", tmp_path / "parsed", digest, config, rules)
    chunk_ref = ArtifactStore(tmp_path / "chunks").put_json(chunks)
    assert (
        checked_chunks(
            tmp_path / "raw", tmp_path / "parsed", tmp_path / "chunks", chunk_ref["sha256"]
        )
        == chunks
    )
    focus = next(n for n in chunks["chunks"] if n["searchable"])
    expanded = expand_chunk(chunks, focus["chunk_id"])
    texts = [n["text"] for n in [expanded["focus"], *expanded["context"]]]
    assert row["Consequence"] in texts and row["Remedy"] in texts
    rejected_changes = 0
    for node in [expanded["focus"], *expanded["context"]]:
        for value in cite_chunk(tmp_path / "raw", tmp_path / "parsed", chunks, node["chunk_id"]):
            original = Citation.model_validate(value)
            assert (
                resolve_citation(tmp_path / "raw", tmp_path / "parsed", original)["status"]
                == "verified"
            )
            if "do not" in original.quote:
                # Even rehashing the modified quote cannot change the pinned original text.
                quote = original.quote.replace("do not", "do")
                changed = original.model_copy(
                    update={"quote": quote, "quote_sha256": text_hash(quote)}
                )
                with pytest.raises(ArtifactIntegrityError):
                    resolve_citation(tmp_path / "raw", tmp_path / "parsed", changed)
                rejected_changes += 1
    assert rejected_changes > 0
    assert coverage_summary(parsed, chunks)["root_only_ranges"] == []


def test_new_content_at_same_url_keeps_old_negation_and_chunk_citation(tmp_path):
    raw = tmp_path / "raw"
    first = capture_rows(raw, [complaint(summary="SYNTHETIC not a confirmed defect")])
    result = audit(tmp_path, [request(first)])
    item = result["items"][0]
    digest = item["chunk_attempts"][0]["chunk_artifact"]["sha256"]
    old = checked_chunks(raw, tmp_path / "parsed", tmp_path / "chunks", digest)
    leaf = next(n for n in old["chunks"] if n["searchable"])
    citation = Citation.model_validate(
        cite_chunk(raw, tmp_path / "parsed", old, leaf["chunk_id"])[0]
    )
    newer = capture_rows(raw, [complaint(summary="SYNTHETIC different newer narrative")])
    new_item = audit(tmp_path, [request(newer)])["items"][0]
    assert item["source"]["source_url"] == new_item["source"]["source_url"]
    assert item["source"]["source_version_id"] != new_item["source"]["source_version_id"]
    assert checked_chunks(raw, tmp_path / "parsed", tmp_path / "chunks", digest) == old
    assert "not a confirmed defect" in citation.quote
    assert (
        resolve_citation(raw, tmp_path / "parsed", citation)["source"]["snapshot_id"]
        == first.snapshot_id
    )


def test_missing_required_narrative_is_rejected_before_parse_not_zero_success(tmp_path):
    snapshot = capture_rows(tmp_path / "raw", [complaint(summary=None)])
    result = audit(tmp_path, [request(snapshot)])
    assert result["failures"][0]["code"] == "source_record_rejected"
    assert result["failures"][0]["stage"] == "source"
    assert result["items"][0]["parse_status"] == "not_parsed"
    assert result["counts"]["chunk_sets"] == 0


def test_invalid_table_template_has_specific_failure_code(tmp_path):
    parsed, _ = saved_parse(tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC WRONG HEADER"])
    rules = profiles(parsed).model_dump(mode="json")
    part = rules["documents"][0]["sections"][1]["parts"][0]
    part["kind"] = "table"
    part["table"] = {
        "columns": ["ODI", "Total"],
        "row_labels": ["Complaints:"],
        "footnote_prefix": "*Other:",
    }
    result = audit(
        tmp_path,
        [ParseRequest(snapshot_id=parsed["source"]["snapshot_id"])],
        rules=StructureRules.model_validate(rules),
    )
    assert result["failures"][0]["code"] == "chunk_table_shape_changed"
    assert result["failures"][0]["strategy"] == "structure"
    assert result["items"][0]["chunk_attempts"][1]["status"] == "built"

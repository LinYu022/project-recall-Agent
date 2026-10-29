"""Synthetic in-memory PDF/JSON fixtures; no source downloads or document deliverables."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ingestion.snapshots import FetchResult, SnapshotStore
from ingestion.sources import load_sources
from parsing.__main__ import main
from parsing.citations import checked_parse, make_citation, resolve_citation
from parsing.contracts import ParseConfig, ParseRequest
from parsing.parser import make_unit, parse_source, text_hash
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.source_fixtures import capture_rows, complaint, recall


def pdf_bytes(texts, *, encrypted=False):
    """Small fixture byte streams only, not authored user PDF artifacts."""
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        if text is not None:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(("BT /F1 12 Tf 40 740 Td (" + escaped + ") Tj ET").encode("ascii"))
            page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("SYNTHETIC-PASSWORD")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def capture_pdf(root, body):
    spec = load_sources()["NHTSA-INV-PE10006-CLOSE"]
    return SnapshotStore(root).capture(spec, lambda _: FetchResult(200, {}, body))


def stored(root, text="SYNTHETIC explicit restriction not every vehicle"):
    snapshot = capture_pdf(root / "raw", pdf_bytes([text]))
    doc = parse_source(root / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id))
    ref = ArtifactStore(root / "parsed").put_json(doc)
    return snapshot, doc, ref["sha256"]


def test_pdf_page_text_locations_and_recipe_are_stable(tmp_path):
    snapshot, parsed, digest = stored(tmp_path)
    unit = parsed["units"][0]
    assert parsed["status"] == "parsed" and parsed["total_pdf_pages"] == 1
    assert unit["page_number"] == 1 and unit["text"].startswith("SYNTHETIC")
    assert (
        unit["reading_order"] == "unverified_layout" and unit["table_semantics"] == "not_inferred"
    )
    assert unit["regions"][0]["parent_unit_id"] == unit["unit_id"]
    assert ArtifactStore(tmp_path / "parsed").put_json(parsed)["sha256"] == digest
    assert parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id)) == parsed


def test_blank_line_regions_keep_exact_offsets_and_parent():
    text = "  Heading \n\n  First line\n  continued.  \n\n\n Last condition\n"
    unit = make_unit(text, page=1, field_pointer=None)
    assert unit["text"] == text
    assert [text[r["start"] : r["end"]] for r in unit["regions"]] == [
        "Heading",
        "First line\n  continued.",
        "Last condition",
    ]
    assert all(r["text_sha256"] == text_hash(text[r["start"] : r["end"]]) for r in unit["regions"])


def test_short_complaint_preserves_the_entire_original_narrative(tmp_path):
    raw = "  SYNTHETIC café\n\nNot every vehicle.  "
    snapshot = capture_rows(tmp_path, [complaint(summary=raw)])
    doc = parse_source(tmp_path, ParseRequest(snapshot_id=snapshot.snapshot_id))
    unit = doc["units"][0]
    assert unit["text"] == raw and unit["field_pointer"] == "/results/0/summary"
    assert len(unit["regions"]) == 1
    assert unit["regions"][0]["start"] == 0 and unit["regions"][0]["end"] == len(raw)
    assert unit["page_number"] is None


def test_json_citation_uses_character_offsets_and_has_no_pdf_runtime_dependency(
    tmp_path, monkeypatch
):
    raw = "SYNTHETIC café 🚗 remains unknown"
    snapshot = capture_rows(tmp_path / "raw", [complaint(summary=raw)])
    parsed = parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id))
    digest = ArtifactStore(tmp_path / "parsed").put_json(parsed)["sha256"]
    start, quote = raw.index("café"), "café 🚗"
    citation = make_citation(
        tmp_path / "raw", tmp_path / "parsed", digest, 0, start, start + len(quote)
    )
    assert citation.quote == quote and citation.end - citation.start != len(quote.encode("utf-8"))
    monkeypatch.setattr("pypdf.__version__", "SYNTHETIC-FUTURE")
    result = resolve_citation(tmp_path / "raw", tmp_path / "parsed", citation)
    assert result["status"] == "verified" and result["field_pointer"] == "/results/0/summary"


def test_recall_keeps_consequence_and_remedy_separate_from_summary(tmp_path):
    row = recall() | {"Consequence": "SYNTHETIC risk", "Remedy": "SYNTHETIC limited remedy"}
    snapshot = capture_rows(tmp_path, [row], kind="recall_api")
    doc = parse_source(tmp_path, ParseRequest(snapshot_id=snapshot.snapshot_id))
    assert [u["field_pointer"] for u in doc["units"]] == [
        "/results/0/Summary",
        "/results/0/Consequence",
        "/results/0/Remedy",
    ]


@pytest.mark.parametrize(
    "texts,expected", [([None], "needs_review_or_ocr"), (["SYNTHETIC text", None], "partial")]
)
def test_no_text_is_not_a_successful_empty_document(tmp_path, texts, expected):
    snapshot = capture_pdf(tmp_path / "raw", pdf_bytes(texts))
    doc = parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id))
    assert doc["status"] == expected and doc["ocr_performed"] is False
    assert doc["issues"][-1]["code"] == "no_extractable_text_needs_review_or_ocr"
    digest = ArtifactStore(tmp_path / "parsed").put_json(doc)["sha256"]
    with pytest.raises(ValueError, match="incomplete"):
        make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, 0, 1)


def test_encrypted_and_damaged_pdf_are_explicit_failures(tmp_path):
    for body, code in [
        (pdf_bytes(["SYNTHETIC"], encrypted=True), "encrypted_pdf_not_supported"),
        (b"%PDF-SYNTHETIC BROKEN", "pdf_parse_failed"),
    ]:
        snapshot = capture_pdf(tmp_path, body)
        doc = parse_source(tmp_path, ParseRequest(snapshot_id=snapshot.snapshot_id))
        assert doc["status"] == "failed" and doc["units"] == []
        assert doc["issues"][0]["code"] == code


def test_page_and_text_limits_refuse_partial_truncation_as_complete(tmp_path):
    snapshot = capture_pdf(tmp_path, pdf_bytes(["SYNTHETIC first", "SYNTHETIC second"]))
    request = ParseRequest(snapshot_id=snapshot.snapshot_id)
    for config, code in [
        (ParseConfig(max_pages=1), "page_limit_or_empty_document"),
        (ParseConfig(max_text_characters=5), "text_limit_exceeded"),
    ]:
        doc = parse_source(tmp_path, request, config)
        assert doc["status"] == "failed" and doc["units"] == []
        assert doc["issues"][0]["code"] == code


def test_citation_replays_raw_parse_and_exact_excerpt(tmp_path):
    _, doc, digest = stored(tmp_path)
    text = doc["units"][0]["text"]
    start = text.index("not every vehicle")
    citation = make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, start, len(text))
    result = resolve_citation(tmp_path / "raw", tmp_path / "parsed", citation)
    assert result["status"] == "verified" and result["page_number"] == 1
    assert result["source"]["raw_object_path"].startswith("objects/")
    assert result["verification"].endswith("not_semantic_support")


@pytest.mark.parametrize("change", ["quote", "quote_hash", "range", "source", "unit"])
def test_changed_citation_is_rejected(tmp_path, change):
    _, doc, digest = stored(tmp_path)
    citation = make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, 0, 9)
    changes = {
        "quote": {"quote": "different"},
        "quote_hash": {"quote_sha256": "0" * 64},
        "range": {"end": len(doc["units"][0]["text"]) + 1},
        "source": {"source_version_id": "0" * 64},
        "unit": {"unit_id": "0" * 64},
    }
    with pytest.raises(ArtifactIntegrityError):
        resolve_citation(
            tmp_path / "raw", tmp_path / "parsed", citation.model_copy(update=changes[change])
        )


def test_same_url_new_body_does_not_replace_old_citation(tmp_path):
    first, doc, digest = stored(tmp_path)
    citation = make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, 0, 9)
    second = capture_pdf(tmp_path / "raw", pdf_bytes(["SYNTHETIC a newer and different document"]))
    changed = parse_source(tmp_path / "raw", ParseRequest(snapshot_id=second.snapshot_id))
    assert changed["source"]["source_url"] == doc["source"]["source_url"]
    assert changed["source"]["source_version_id"] != doc["source"]["source_version_id"]
    assert (
        resolve_citation(tmp_path / "raw", tmp_path / "parsed", citation)["source"]["snapshot_id"]
        == first.snapshot_id
    )


def test_tampered_raw_bytes_and_parse_artifact_are_integrity_errors(tmp_path):
    snapshot, _, digest = stored(tmp_path)
    citation = make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, 0, 9)
    raw_path = tmp_path / "raw" / "objects" / snapshot.raw.sha256
    raw = raw_path.read_bytes()
    raw_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        resolve_citation(tmp_path / "raw", tmp_path / "parsed", citation)
    raw_path.write_bytes(raw)
    (tmp_path / "parsed" / "artifacts" / (digest + ".json")).write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        resolve_citation(tmp_path / "raw", tmp_path / "parsed", citation)


def test_changed_parser_runtime_requires_the_recorded_environment(tmp_path, monkeypatch):
    _, _, digest = stored(tmp_path)
    monkeypatch.setattr("pypdf.__version__", "SYNTHETIC-FUTURE")
    with pytest.raises(ValueError, match="recorded pypdf version"):
        checked_parse(tmp_path / "raw", tmp_path / "parsed", digest)


def test_invalid_source_row_and_whitespace_citation_are_not_valid(tmp_path):
    snapshot, _, digest = stored(tmp_path)
    with pytest.raises(ValueError, match="position"):
        parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id, ordinal=1))
    with pytest.raises(ValueError, match="offsets"):
        make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, -1, 9)


def test_whitespace_only_citation_is_rejected(tmp_path):
    snapshot = capture_rows(tmp_path / "raw", [complaint(summary="  SYNTHETIC  ")])
    parsed = parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id))
    digest = ArtifactStore(tmp_path / "parsed").put_json(parsed)["sha256"]
    with pytest.raises(ValueError, match="whitespace"):
        make_citation(tmp_path / "raw", tmp_path / "parsed", digest, 0, 0, 2)


def test_page_extraction_failure_keeps_original_page_numbers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pypdf.errors import PdfReadError

    def broken(**kwargs):
        raise PdfReadError("SYNTHETIC page failure")

    reader = SimpleNamespace(
        is_encrypted=False,
        pages=[
            SimpleNamespace(extract_text=broken),
            SimpleNamespace(extract_text=lambda **kwargs: "SYNTHETIC second page"),
        ],
    )
    snapshot = capture_pdf(tmp_path, pdf_bytes([None, None]))
    monkeypatch.setattr("parsing.parser.PdfReader", lambda *args, **kwargs: reader)
    parsed = parse_source(tmp_path, ParseRequest(snapshot_id=snapshot.snapshot_id))
    assert parsed["status"] == "partial" and parsed["units"][0]["page_number"] == 2
    assert parsed["issues"] == [{"page_number": 1, "code": "page_extraction_failed"}]


def test_ambiguous_find_does_not_choose_the_first_occurrence(tmp_path, monkeypatch, capsys):
    _, _, digest = stored(tmp_path, "SYNTHETIC repeated repeated")
    monkeypatch.setattr(
        "sys.argv",
        [
            "parsing",
            "cite",
            digest,
            "--find",
            "repeated",
            "--raw-root",
            str(tmp_path / "raw"),
            "--parsed-root",
            str(tmp_path / "parsed"),
        ],
    )
    assert main() == 2
    assert '"status": "parse_operation_failed"' in capsys.readouterr().out


def test_cli_parse_cite_verify_and_duplicate_output_failure(tmp_path, monkeypatch, capsys):
    snapshot = capture_pdf(tmp_path / "raw", pdf_bytes(["SYNTHETIC exact citation"]))
    common = ["--raw-root", str(tmp_path / "raw"), "--parsed-root", str(tmp_path / "parsed")]
    monkeypatch.setattr("sys.argv", ["parsing", "parse", snapshot.snapshot_id, *common])
    assert main() == 0
    import json

    digest = json.loads(capsys.readouterr().out)["parse_artifact"]["sha256"]
    output = tmp_path / "citation.json"
    argv = ["parsing", "cite", digest, "--find", "exact citation", "--output", str(output), *common]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    capsys.readouterr()
    assert main() == 2  # Cannot overwrite a citation file.
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["parsing", "verify", str(output), *common])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def test_cli_no_text_returns_incomplete_exit_code(tmp_path, monkeypatch, capsys):
    snapshot = capture_pdf(tmp_path / "raw", pdf_bytes([None]))
    monkeypatch.setattr(
        "sys.argv",
        [
            "parsing",
            "parse",
            snapshot.snapshot_id,
            "--raw-root",
            str(tmp_path / "raw"),
            "--parsed-root",
            str(tmp_path / "parsed"),
        ],
    )
    assert main() == 4
    assert '"ocr_performed": false' in capsys.readouterr().out

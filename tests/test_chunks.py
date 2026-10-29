"""Synthetic chunk boundaries, table layouts and context/citation failure scenarios."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from parsing.__main__ import main
from parsing.chunk_contracts import ChunkConfig, StructureRules, load_chunk_config
from parsing.chunk_review import render_review
from parsing.chunks import _table, build_chunks, checked_chunks, cite_chunk, expand_chunk
from parsing.citations import resolve_citation
from parsing.contracts import Citation, ParseRequest
from parsing.parser import parse_source, text_hash
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from tests.source_fixtures import capture_rows, complaint, recall
from tests.test_parsing import capture_pdf, pdf_bytes


def saved_parse(tmp_path, *, pages=None, narrative=None):
    raw, parsed_root = tmp_path / "raw", tmp_path / "parsed"
    snapshot = (
        capture_pdf(raw, pdf_bytes(pages))
        if pages is not None
        else capture_rows(raw, [complaint(summary=narrative)])
    )
    parsed = parse_source(raw, ParseRequest(snapshot_id=snapshot.snapshot_id))
    digest = ArtifactStore(parsed_root).put_json(parsed)["sha256"]
    return parsed, digest


def location(unit, start=0, end=None):
    end = len(unit["text"]) if end is None else end
    return dict(
        unit_id=unit["unit_id"],
        start=start,
        end=end,
        text_sha256=text_hash(unit["text"][start:end]),
    )


def profiles(parsed):
    units = parsed["units"]
    return StructureRules.model_validate(
        dict(
            rules_version="SYNTHETIC-layout/0.1",
            documents=[
                dict(
                    source_version_id=parsed["source"]["source_version_id"],
                    parser_sha256=parsed["parser_sha256"],
                    review_note="SYNTHETIC reviewed scope and paragraph across pages",
                    sections=[
                        dict(
                            key="scope",
                            title_path=["SYNTHETIC", "Vehicle scope"],
                            ranges=[location(units[0])],
                            context_keys=[],
                            parts=[dict(kind="metadata", ranges=[location(units[0])])],
                        ),
                        dict(
                            key="summary",
                            title_path=["SYNTHETIC", "Summary"],
                            ranges=[location(u) for u in units[1:]],
                            context_keys=["scope"],
                            parts=[dict(kind="paragraph", ranges=[location(u) for u in units[1:]])],
                        ),
                    ],
                )
            ],
        )
    )


def build(tmp_path, digest, rules=None, config=None):
    return build_chunks(
        tmp_path / "raw",
        tmp_path / "parsed",
        digest,
        config or load_chunk_config("structure"),
        rules or StructureRules(rules_version="SYNTHETIC-empty", documents=[]),
    )


def test_cross_page_part_has_required_scope_and_separate_source_citations(tmp_path):
    parsed, digest = saved_parse(
        tmp_path,
        pages=[
            "SYNTHETIC scope only 2010 Toyota Prius",
            "SYNTHETIC subject vehicles have a condition",
            "SYNTHETIC limited to rough road surfaces",
        ],
    )
    result = build(tmp_path, digest, profiles(parsed))
    focus = next(n for n in result["chunks"] if n["kind"] == "paragraph")
    assert len(focus["ranges"]) == 2
    assert "\n\n" in focus["text"]
    bundle = expand_chunk(result, focus["chunk_id"])
    assert any("only 2010 Toyota Prius" in n["text"] for n in bundle["context"])
    assert not any(n["kind"] == "document" for n in bundle["context"])
    citations = cite_chunk(tmp_path / "raw", tmp_path / "parsed", result, focus["chunk_id"])
    assert [
        resolve_citation(tmp_path / "raw", tmp_path / "parsed", Citation.model_validate(c))[
            "page_number"
        ]
        for c in citations
    ] == [2, 3]


def test_short_complaint_is_identical_whole_text_for_both_strategies(tmp_path):
    text = "  SYNTHETIC café 🚗\n\nnot all vehicles.  "
    _, digest = saved_parse(tmp_path, narrative=text)
    for config in [load_chunk_config("structure"), load_chunk_config("fixed")]:
        result = build(tmp_path, digest, config=config)
        leaves = [n for n in result["chunks"] if n["searchable"]]
        assert len(leaves) == 1 and leaves[0]["text"] == text
        assert result["source_record_context"]["products"][0]["productYear"] == "2016"


def test_long_narrative_structure_keeps_paragraphs_and_complete_parent(tmp_path):
    text = "SYNTHETIC first paragraph\n\nOnly vehicles with an installed accessory."
    _, digest = saved_parse(tmp_path, narrative=text)
    config = load_chunk_config("structure").model_copy(update={"short_complaint_characters": 10})
    result = build(tmp_path, digest, config=config)
    leaves = [n for n in result["chunks"] if n["searchable"]]
    assert len(leaves) == 2
    assert text in [n["text"] for n in expand_chunk(result, leaves[1]["chunk_id"])["context"]]


def test_fixed_windows_overlap_and_cover_all_characters_without_short_tail_duplicate(tmp_path):
    text = "SYNTHETIC ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _, digest = saved_parse(tmp_path, narrative=text)
    config = ChunkConfig(
        config_version="SYNTHETIC-fixed",
        strategy="fixed",
        short_complaint_characters=1,
        fixed_characters=10,
        fixed_overlap=3,
    )
    result = build(tmp_path, digest, config=config)
    leaves = [n for n in result["chunks"] if n["searchable"]]
    assert [n["ranges"][0]["start"] for n in leaves] == [0, 7, 14, 21, 28]
    covered = {i for n in leaves for i in range(n["ranges"][0]["start"], n["ranges"][0]["end"])}
    assert covered == set(range(len(text)))
    assert all(len(n["text"]) <= 10 for n in leaves)


def test_fixed_and_structure_have_same_available_reviewed_scope(tmp_path):
    parsed, digest = saved_parse(
        tmp_path, pages=["SYNTHETIC only 2010 Prius", "SYNTHETIC subject vehicles"]
    )
    rules = profiles(parsed)
    for config in [load_chunk_config("structure"), load_chunk_config("fixed")]:
        result = build(tmp_path, digest, rules, config)
        focus = next(
            n for n in result["chunks"] if n["searchable"] and "subject vehicles" in n["text"]
        )
        assert any(
            "only 2010 Prius" in n["text"]
            for n in expand_chunk(result, focus["chunk_id"])["context"]
        )


def test_unreviewed_pdf_refuses_structured_chunks_but_can_build_labeled_baseline(tmp_path):
    _, digest = saved_parse(tmp_path, pages=["SYNTHETIC unfamiliar layout"])
    with pytest.raises(ValueError, match="matching reviewed"):
        build(tmp_path, digest)
    baseline = build(tmp_path, digest, config=load_chunk_config("fixed"))
    assert baseline["context_status"] == "layout_not_reviewed"


def test_new_content_at_same_url_does_not_inherit_old_layout_rules(tmp_path):
    parsed, digest = saved_parse(tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC old paragraph"])
    rules = profiles(parsed)
    snapshot = capture_pdf(
        tmp_path / "raw", pdf_bytes(["SYNTHETIC new scope", "SYNTHETIC new paragraph"])
    )
    changed = parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id))
    assert changed["source"]["source_url"] == parsed["source"]["source_url"]
    changed_digest = ArtifactStore(tmp_path / "parsed").put_json(changed)["sha256"]
    with pytest.raises(ValueError, match="matching reviewed"):
        build(tmp_path, changed_digest, rules)
    assert build(tmp_path, digest, rules)["context_status"] == "reviewed_layout_context"


def test_overlapping_sections_are_refused_for_both_recipes(tmp_path):
    parsed, digest = saved_parse(tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC paragraph"])
    values = profiles(parsed).model_dump(mode="json")
    values["documents"][0]["sections"][1]["ranges"] = [location(parsed["units"][0])]
    for config in [load_chunk_config("structure"), load_chunk_config("fixed")]:
        with pytest.raises(ValueError, match="sections must not overlap"):
            build(tmp_path, digest, StructureRules.model_validate(values), config)


def test_partial_parsing_cannot_be_used_as_complete_chunks(tmp_path):
    _, digest = saved_parse(tmp_path, pages=["SYNTHETIC success", None])
    with pytest.raises(ValueError, match="complete parsed text"):
        build(tmp_path, digest, config=load_chunk_config("fixed"))


@pytest.mark.parametrize("change", ["unknown_context", "cycle", "duplicate"])
def test_bad_structure_graphs_are_rejected(tmp_path, change):
    parsed, _ = saved_parse(tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC paragraph"])
    values = profiles(parsed).model_dump(mode="json")
    sections = values["documents"][0]["sections"]
    if change == "unknown_context":
        sections[1]["context_keys"] = ["missing"]
    elif change == "cycle":
        sections[0]["context_keys"] = ["summary"]
    else:
        sections.append(deepcopy(sections[0]))
    with pytest.raises(ValidationError):
        StructureRules.model_validate(values)


@pytest.mark.parametrize(
    "parameters",
    [
        dict(fixed_characters=10, fixed_overlap=10),
        dict(fixed_characters=10, fixed_overlap=11),
        dict(fixed_characters=10, fixed_overlap=None),
    ],
)
def test_fixed_parameters_require_positive_progress(parameters):
    with pytest.raises(ValidationError):
        ChunkConfig(config_version="SYNTHETIC", strategy="fixed", **parameters)


def test_layout_range_hash_and_parent_bounds_are_checked(tmp_path):
    parsed, digest = saved_parse(tmp_path, pages=["SYNTHETIC scope", "SYNTHETIC paragraph"])
    values = profiles(parsed).model_dump(mode="json")
    values["documents"][0]["sections"][1]["parts"][0]["ranges"][0]["text_sha256"] = "0" * 64
    with pytest.raises(ArtifactIntegrityError):
        build(tmp_path, digest, StructureRules.model_validate(values))
    values = profiles(parsed).model_dump(mode="json")
    values["documents"][0]["sections"][1]["parts"][0]["ranges"] = [location(parsed["units"][0])]
    with pytest.raises(ValueError, match="inside its section"):
        build(tmp_path, digest, StructureRules.model_validate(values))


def test_table_header_values_and_footnote_are_bound_to_cell_positions():
    raw = "ODI  Manufacturer  Total\nComplaints:   1,235  192   1,427\n*Other: Warranty claims"
    template = dict(
        columns=["ODI", "Manufacturer", "Total"],
        row_labels=["Complaints:"],
        footnote_prefix="*Other:",
    )
    table = _table(raw, template, {"unit_id": "a" * 64, "start": 40})
    assert table["rows"][0]["values"] == [1235, 192, 1427]
    for cell in table["cells"]:
        assert raw[cell["start"] - 40 : cell["end"] - 40] == cell["raw"]
    assert table["footnote"] == "*Other: Warranty claims"


@pytest.mark.parametrize(
    "text",
    [
        "wrong header\nComplaints: 1 2 3\n*Other: note",
        "ODI Manufacturer Total\nComplaints: 1 ? 3\n*Other: note",
        "ODI Manufacturer Total\nComplaints: 1 2 3\nmissing footnote",
    ],
)
def test_changed_table_shape_fails_instead_of_guessing_columns(text):
    with pytest.raises(ValueError, match="table"):
        _table(
            text,
            dict(
                columns=["ODI", "Manufacturer", "Total"],
                row_labels=["Complaints:"],
                footnote_prefix="*Other:",
            ),
            {"unit_id": "a" * 64, "start": 0},
        )


def test_saved_chunk_replay_is_idempotent_and_modified_recipe_is_rejected(tmp_path):
    _, digest = saved_parse(tmp_path, narrative="SYNTHETIC original field")
    result = build(tmp_path, digest)
    store = ArtifactStore(tmp_path / "chunks")
    ref = store.put_json(result)
    assert store.put_json(result) == ref
    assert (
        checked_chunks(tmp_path / "raw", tmp_path / "parsed", tmp_path / "chunks", ref["sha256"])
        == result
    )
    changed = deepcopy(result)
    changed["chunks"][-1]["required_context_ids"] = []
    changed["chunks"][-1]["text"] = "SYNTHETIC malicious replacement"
    modified = store.put_json(changed)
    with pytest.raises(ArtifactIntegrityError):
        checked_chunks(
            tmp_path / "raw", tmp_path / "parsed", tmp_path / "chunks", modified["sha256"]
        )


def test_recall_remedy_keeps_summary_scope_context(tmp_path):
    snapshot = capture_rows(
        tmp_path / "raw",
        [recall() | {"Summary": "SYNTHETIC limited vehicles", "Remedy": "SYNTHETIC replace it"}],
        kind="recall_api",
    )
    parsed = parse_source(tmp_path / "raw", ParseRequest(snapshot_id=snapshot.snapshot_id))
    digest = ArtifactStore(tmp_path / "parsed").put_json(parsed)["sha256"]
    for config in [load_chunk_config("structure"), load_chunk_config("fixed")]:
        result = build(tmp_path, digest, config=config)
        remedy = next(n for n in result["chunks"] if n["searchable"] and "replace it" in n["text"])
        assert any(
            "limited vehicles" in n["text"]
            for n in expand_chunk(result, remedy["chunk_id"])["context"]
        )


def test_review_html_escapes_untrusted_text_and_refuses_overwrite(tmp_path):
    _, digest = saved_parse(tmp_path, narrative="SYNTHETIC <script>bad()</script>")
    structured = build(tmp_path, digest)
    fixed = build(tmp_path, digest, config=load_chunk_config("fixed"))
    path = tmp_path / "review.html"
    render_review(structured, fixed, path)
    html = path.read_text(encoding="utf-8")
    assert "<script>" not in html and "&lt;script&gt;" in html
    with pytest.raises(FileExistsError):
        render_review(structured, fixed, path)


def test_cli_chunk_does_not_fall_through_into_citation_file_read(tmp_path, monkeypatch, capsys):
    _, digest = saved_parse(tmp_path, narrative="SYNTHETIC CLI data")
    common = [
        "--raw-root",
        str(tmp_path / "raw"),
        "--parsed-root",
        str(tmp_path / "parsed"),
        "--chunk-root",
        str(tmp_path / "chunks"),
    ]
    monkeypatch.setattr("sys.argv", ["parsing", "chunk", digest, *common])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "chunked"
    chunk_set = ArtifactStore(tmp_path / "chunks").read_json(result["chunk_set_artifact"]["sha256"])
    leaf = next(n for n in chunk_set["chunks"] if n["searchable"])
    monkeypatch.setattr(
        "sys.argv",
        [
            "parsing",
            "chunk-cite",
            result["chunk_set_artifact"]["sha256"],
            "--chunk-id",
            leaf["chunk_id"],
            *common,
        ],
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "located"

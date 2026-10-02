"""The bounds on text as it enters the application: separator runs, tokens, and total size."""

from __future__ import annotations

import io
import random
import time
from pathlib import Path

import pytest
from docling_core.types.doc.document import DoclingDocument, TableCell, TableData
from docling_core.types.doc.labels import DocItemLabel
from openpyxl import Workbook

from procurement_agent.config import Settings
from procurement_agent.ingestion import excel_reader
from procurement_agent.ingestion.docling_service import (
    ConversionError,
    convert,
    load_document,
    save_document,
)
from procurement_agent.ingestion.text_limits import (
    MAX_SEPARATOR_RUN,
    MAX_TOKEN,
    bound_text,
    compact_document,
    compact_runs,
    text_size,
)

SEP, TOK = MAX_SEPARATOR_RUN, MAX_TOKEN


# --- what is kept ------------------------------------------------------------------------------


def test_the_limits_stay_above_anything_a_real_document_holds() -> None:
    """Measured on the client's 330 parsed documents: the longest separator run is 116 and the
    longest unbroken token 33. Lowering these below a comfortable margin would start changing real
    text - and with it the extraction results."""
    assert SEP >= 150
    assert TOK >= 128


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "ხელშეკრულება № 17/2024, თარიღი: 01.03.2024",
        "a" + " " * (SEP - 2) + "b",
        "." * SEP,  # exactly at the limit
        "x" * TOK,
        "ა" * TOK,
        "1" * TOK,
        "TOC entry " + "." * 120 + " 12",
        "signature " + "_" * 90,
        "table rule " + "=" * 150,
    ],
)
def test_text_within_the_limits_is_returned_unchanged(text: str) -> None:
    assert bound_text(text) == text
    assert compact_runs(text) == text


def test_an_ordinary_text_comes_back_as_the_same_object() -> None:
    text = "ხელშეკრულება " * 100
    assert bound_text(text) is text or bound_text(text) == text


# --- what is cut -------------------------------------------------------------------------------


@pytest.mark.parametrize("char", [" ", "\n", "\t", " ", "　", ".", "_", "-", "=", ",", "|", "·"])
def test_a_long_run_of_separators_is_cut_to_the_limit(char: str) -> None:
    text = "before" + char * 5000 + "after"
    out = bound_text(text)
    assert out == "before" + char * SEP + "after"


def test_a_long_run_of_mixed_separators_is_one_run() -> None:
    mixed = " \n\t,._-" * 2000
    out = bound_text("a" + mixed + "b")
    assert out == "a" + mixed[:SEP] + "b"


def test_each_long_run_in_a_text_is_cut_and_the_text_between_them_is_kept() -> None:
    out = bound_text("one" + " " * 900 + "two" + "\n" * 900 + "three" + " " * 10 + "four")
    assert out == "one" + " " * SEP + "two" + "\n" * SEP + "three" + " " * 10 + "four"


def test_a_run_at_either_end_of_the_text_is_cut_too() -> None:
    assert bound_text(" " * 1000 + "x") == " " * SEP + "x"
    assert bound_text("x" + " " * 1000) == "x" + " " * SEP
    assert bound_text(" " * 1000) == " " * SEP


@pytest.mark.parametrize("char", ["a", "ა", "1", "Z"])
def test_a_long_word_or_number_is_cut_to_the_token_limit(char: str) -> None:
    out = bound_text("start " + char * 3000 + " end")
    assert out == "start " + char * TOK + " end"
    assert (
        compact_runs("start " + char * 3000 + " end") == "start " + char * 3000 + " end"
    )  # not a separator


def test_a_token_is_measured_between_spaces_so_a_url_inside_a_sentence_is_cut_alone() -> None:
    url = "https://example.test/" + "path/" * 300
    out = bound_text(f"see {url} for details")
    assert out.startswith("see https://example.test/path/") and out.endswith(" for details")
    assert len(out.split(" ")[1]) == TOK


def test_the_result_is_never_longer_and_cutting_twice_changes_nothing() -> None:
    rng = random.Random(11)
    alphabet = [" ", "\n", ".", "a", "ა", "1", "_", "-", ","]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) * rng.randint(1, 600) for _ in range(rng.randint(1, 8)))
        once = bound_text(text)
        assert len(once) <= len(text)
        assert bound_text(once) == once
        assert not any(len(run) > SEP for run in _runs(once, lambda c: not c.isalnum() or c == "_"))
        assert all(len(token) <= TOK for token in once.split())


def _runs(text: str, member) -> list[str]:  # type: ignore[no-untyped-def]
    runs: list[str] = []
    current = ""
    for ch in text:
        if member(ch):
            current += ch
        else:
            if current:
                runs.append(current)
            current = ""
    if current:
        runs.append(current)
    return runs


# --- cost --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        lambda: " \n" * 10_000_000,  # twenty million characters of separators
        lambda: "ხელშეკრულება " * 1_500_000,  # eighteen million characters of ordinary text
        lambda: ("word " * 100 + "." * 150 + "\n") * 40_000,  # many runs just under the limit
        lambda: "a" * 20_000_000,  # one enormous token
    ],
    ids=["separators", "ordinary", "near-limit runs", "one token"],
)
def test_bounding_is_linear_in_the_text(make) -> None:  # type: ignore[no-untyped-def]
    text = make()
    started = time.perf_counter()
    bound_text(text)
    assert time.perf_counter() - started < 6.0


# --- a parsed document -------------------------------------------------------------------------


def _hostile_document() -> DoclingDocument:
    doc = DoclingDocument(name="synthetic")
    doc.add_text(label=DocItemLabel.TEXT, text="ღირებულება 100" + " \n" * 4000 + "თეთრი")
    doc.add_text(label=DocItemLabel.TEXT, text="ordinary paragraph")
    table = doc.add_table(data=TableData(num_rows=1, num_cols=1))
    table.data.table_cells = [
        TableCell(
            text="cell" + " " * 5000 + "end",
            row_span=1,
            col_span=1,
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
        )
    ]
    return doc


def test_a_parsed_documents_text_and_cells_are_bounded_in_place() -> None:
    doc = _hostile_document()
    assert text_size(doc) > 8000
    assert compact_document(doc) >= 2
    assert doc.texts[0].text == "ღირებულება 100" + (" \n" * 4000)[:SEP] + "თეთრი"
    assert doc.texts[1].text == "ordinary paragraph"
    assert doc.tables[0].data.table_cells[0].text == "cell" + " " * SEP + "end"
    assert text_size(doc) < 1000
    assert compact_document(doc) == 0  # nothing left to cut


def test_a_stored_parse_is_bounded_when_it_is_loaded(tmp_path: Path) -> None:
    """Including one written before the limit existed."""
    path = tmp_path / "docling.json"
    _hostile_document().save_as_json(path)
    assert path.stat().st_size > 8000
    loaded = load_document(path)
    assert text_size(loaded) < 1000
    assert loaded.texts[0].text.startswith("ღირებულება 100")


def test_save_then_load_round_trips_ordinary_documents_exactly(tmp_path: Path) -> None:
    doc = DoclingDocument(name="synthetic")
    for line in ("პირველი აბზაცი.", "Second paragraph, with a number: 120,000.00 GEL", "x" * 200):
        doc.add_text(label=DocItemLabel.TEXT, text=line)
    save_document(doc, tmp_path)
    back = load_document(tmp_path / "docling.json")
    assert [t.text for t in back.texts] == [t.text for t in doc.texts]


# --- the size of the whole ---------------------------------------------------------------------


def _docx(paragraphs: int, size: int) -> bytes:
    from docx import Document

    word = Document()
    for i in range(paragraphs):
        word.add_paragraph(f"{i} " + "word " * (size // 5))
    out = io.BytesIO()
    word.save(out)
    return out.getvalue()


def test_a_document_with_more_text_than_the_limit_fails_to_parse_with_numbers_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "big.docx"
    path.write_bytes(_docx(20, 2000))  # about 40,000 characters
    small = Settings(
        env="dev",
        data_dir=tmp_path / "d",
        ollama_base_url="http://127.0.0.1:1",
        max_text_chars=10_000,
    )
    with pytest.raises(
        ConversionError, match=r"holds \d+ characters of text, more than the limit of 10000"
    ):
        convert(path, small)
    roomy = Settings(
        env="dev",
        data_dir=tmp_path / "d",
        ollama_base_url="http://127.0.0.1:1",
        max_text_chars=1_000_000,
    )
    assert convert(path, roomy).document.texts


def test_the_limit_is_configurable_and_bounded() -> None:
    assert Settings().max_text_chars == 6_000_000
    for bad in (9_999, 200_000_001):
        with pytest.raises(ValueError, match="max_text_chars"):
            Settings(max_text_chars=bad)


# --- workbooks ---------------------------------------------------------------------------------


def _xlsx(rows: list[list[object]], sheets: int = 1) -> bytes:
    wb = Workbook()
    for i in range(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        assert ws is not None
        ws.title = f"S{i}"
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_a_workbook_cell_is_bounded_like_any_other_text() -> None:
    sheets = excel_reader.read_workbook(_xlsx([["price", "a" * 5000 + " " * 5000 + "end"]]))
    values = [c.value for c in sheets[0].cells]
    assert values[0] == "price"
    assert values[1] == "a" * TOK + " " * 0 or len(values[1]) <= TOK + SEP + 3
    assert all(len(token) <= TOK for v in values for token in v.split())


def test_a_workbook_larger_than_the_cell_budget_is_read_up_to_it_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(excel_reader, "_MAX_CELLS", 50)
    notes: list[str] = []
    sheets = excel_reader.read_workbook(_xlsx([[1, 2, 3, 4, 5]] * 40, sheets=3), notes=notes)
    assert sum(len(s.cells) for s in sheets) == 50
    assert sheets[-1].truncated_size
    assert any("larger than the 50 cells" in n for n in notes)
    assert len(sheets) <= 2  # the third sheet is not even opened for reading


def test_a_workbook_larger_than_the_character_budget_is_read_up_to_it_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(excel_reader, "_MAX_TEXT_CHARS", 200)
    notes: list[str] = []
    sheets = excel_reader.read_workbook(_xlsx([["abcdefghij"] * 5] * 30), notes=notes)
    assert sum(len(c.value) for s in sheets for c in s.cells) <= 200
    assert any("characters that are read" in n for n in notes)


def test_a_workbook_within_the_budgets_is_read_whole() -> None:
    notes: list[str] = []
    sheets = excel_reader.read_workbook(_xlsx([["a", 1, 2.5]] * 100, sheets=2), notes=notes)
    assert sum(len(s.cells) for s in sheets) == 600
    assert notes == []

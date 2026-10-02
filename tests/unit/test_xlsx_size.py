"""Counting an .xlsx's cells from the archive, without opening it as a workbook.

A full read builds an object for every cell (about 420 bytes each) and the BOQ reader does it
twice; the upload guard bounds inflated bytes, not cells. So the count has to be cheap, exact
enough, and available before any cell is built.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from procurement_agent.checks import boq as boq_module
from procurement_agent.checks.boq import read_bid, read_boq
from procurement_agent.config import Settings
from procurement_agent.ingestion import xlsx_size
from procurement_agent.ingestion.xlsx_size import (
    DEFAULT_MAX_CELLS,
    WorkbookTooLargeError,
    count_cells,
    ensure_cell_count,
)


def _workbook(tmp_path: Path, rows: int, cols: int, sheets: int = 1) -> Path:
    wb = Workbook()
    for index in range(sheets):
        ws = wb.active if index == 0 else wb.create_sheet()
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                ws.cell(row=r, column=c, value=r * c)
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return path


def _archive(tmp_path: Path, **parts: bytes) -> Path:
    path = tmp_path / "parts.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name.replace("__", "/").replace("_dot_", "."), data)
    return path


def test_the_count_is_the_number_of_cells(tmp_path: Path) -> None:
    assert count_cells(_workbook(tmp_path, 10, 5), 10**9) == 50


def test_the_count_adds_up_over_the_sheets(tmp_path: Path) -> None:
    assert count_cells(_workbook(tmp_path, 4, 4, sheets=3), 10**9) == 48


def test_the_count_stops_once_the_limit_is_passed(tmp_path: Path) -> None:
    assert count_cells(_workbook(tmp_path, 100, 100), 10) > 10


def test_elements_that_only_start_with_c_are_no_cells(tmp_path: Path) -> None:
    other = b'<cols><col min="1"/></cols><cfRule type="x"/><c:chart/><cellXfs count="1"/>'
    assert count_cells(_archive(tmp_path, xl__a_dot_xml=other), 10**9) == 0
    cells = b'<c r="A1"/><c><c r="B2" t="s"><v>1</v></c>'
    assert count_cells(_archive(tmp_path, xl__a_dot_xml=cells), 10**9) == 3


def test_a_part_that_is_not_xml_is_not_counted(tmp_path: Path) -> None:
    assert count_cells(_archive(tmp_path, xl__blob_dot_bin=b'<c r="A1"/>' * 5), 10**9) == 0


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 5, 7, 16])
def test_a_cell_cut_by_a_chunk_edge_is_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(xlsx_size, "_CHUNK", chunk)
    body = b"".join(b'<c r="A%d"/>xx' % i for i in range(20))
    assert count_cells(_archive(tmp_path, xl__sheet_dot_xml=body), 10**9) == 20


def test_ensure_refuses_above_the_limit_and_says_only_how_many(tmp_path: Path) -> None:
    path = _workbook(tmp_path, 10, 10)
    ensure_cell_count(path, 100)  # at the limit: fine
    with pytest.raises(WorkbookTooLargeError, match="more than 99 cells"):
        ensure_cell_count(path, 99)


def test_a_file_that_is_no_archive_is_left_to_the_reader(tmp_path: Path) -> None:
    path = tmp_path / "not.xlsx"
    path.write_bytes(b"this is not a zip archive")
    ensure_cell_count(path, 10)  # no error here; the reader refuses it in its own words


def test_the_default_limit_is_the_setting(tmp_path: Path) -> None:
    assert Settings(data_dir=tmp_path).boq_max_cells == DEFAULT_MAX_CELLS


def test_read_boq_refuses_before_any_cell_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_load(*args: object, **kwargs: object) -> object:
        raise AssertionError("the workbook was opened although it is over the limit")

    monkeypatch.setattr(boq_module, "load_workbook", must_not_load)
    path = _workbook(tmp_path, 40, 30)  # 1 200 cells
    with pytest.raises(WorkbookTooLargeError):
        read_boq(path, name="big", max_cells=1000)


def test_a_bid_over_the_limit_is_reported_unreadable(tmp_path: Path) -> None:
    bid = read_bid(_workbook(tmp_path, 40, 30), name="Big Co", max_cells=1000)
    assert bid.read_error == "WorkbookTooLargeError"

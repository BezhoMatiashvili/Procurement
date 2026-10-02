"""A PDF line's text in the order it is read, left to right, whatever order the file wrote its
runs in (``ingestion.docling_service.in_line_order``): a writer that draws Georgian letters in
one font and digits in another may write a line's runs out of order (Test Set 4's PDF acts).
Invented text only.
"""

from __future__ import annotations

from docling.datamodel.base_models import InputFormat
from docling_core.types.doc.base import CoordOrigin
from docling_core.types.doc.page import BoundingRectangle, TextCell, TextDirection

from procurement_agent.config import Settings
from procurement_agent.ingestion.docling_service import (
    LineOrderedPdfBackend,
    _build,
    in_line_order,
    mend_interleaved,
)


def _cell(
    index: int,
    text: str,
    left: float,
    top: float,
    *,
    height: float = 12.0,
    rtl: bool = False,
) -> TextCell:
    """A cell on a page whose origin is its top left, ``len(text) * 6`` points wide."""
    right, bottom = left + 6.0 * len(text), top + height
    return TextCell(
        index=index,
        rect=BoundingRectangle(
            r_x0=left,
            r_y0=top,
            r_x1=right,
            r_y1=top,
            r_x2=right,
            r_y2=bottom,
            r_x3=left,
            r_y3=bottom,
            coord_origin=CoordOrigin.TOPLEFT,
        ),
        text=text,
        orig=text,
        text_direction=TextDirection.RIGHT_TO_LEFT if rtl else TextDirection.LEFT_TO_RIGHT,
        from_ocr=False,
    )


def _read(cells: list[TextCell]) -> list[str]:
    return [c.text for c in sorted(cells, key=lambda c: c.index)]


def test_a_line_written_out_of_its_order_reads_left_to_right() -> None:
    """The letters' runs first, the digits' after, as a font fallback writes them."""
    cells = [
        _cell(0, "წლის", 60.0, 100.0),
        _cell(1, "აგვისტო", 110.0, 100.0),
        _cell(2, "2026", 30.0, 100.0),
        _cell(3, "21", 90.0, 100.0),
    ]
    in_line_order(cells)
    assert _read(cells) == ["2026", "წლის", "21", "აგვისტო"]
    assert [c.text for c in cells] == ["2026", "წლის", "21", "აგვისტო"]


def test_each_line_keeps_its_place_and_reads_left_to_right() -> None:
    cells = [
        _cell(0, "ბ", 50.0, 100.0),
        _cell(1, "ა", 20.0, 100.0),
        _cell(2, "დ", 50.0, 120.0),
        _cell(3, "გ", 20.0, 120.0),
    ]
    in_line_order(cells)
    assert _read(cells) == ["ა", "ბ", "გ", "დ"]


def test_a_page_written_in_order_is_left_as_it_is() -> None:
    """Near miss: two columns written one after the other keep their order - a line holding
    both is ordered within itself, and the columns' own lines stay where they were."""
    cells = [
        _cell(0, "მარცხენა1", 20.0, 100.0),
        _cell(1, "მარცხენა2", 20.0, 120.0),
        _cell(2, "მარჯვენა1", 300.0, 100.0),
        _cell(3, "მარჯვენა2", 300.0, 120.0),
    ]
    before = [(c.index, c.text) for c in cells]
    in_line_order(cells)
    left = [c for c in cells if c.text.startswith("მარცხენა")]
    right = [c for c in cells if c.text.startswith("მარჯვენა")]
    assert [c.text for c in sorted(left, key=lambda c: c.index)] == ["მარცხენა1", "მარცხენა2"]
    assert [c.text for c in sorted(right, key=lambda c: c.index)] == ["მარჯვენა1", "მარჯვენა2"]
    assert sorted(i for i, _ in before) == sorted(c.index for c in cells)


def test_a_superscript_stays_on_its_line() -> None:
    """A run set a little higher - half its height still on the line - is on the line."""
    cells = [_cell(0, "N", 60.0, 96.0), _cell(1, "ფართი", 20.0, 100.0)]
    in_line_order(cells)
    assert _read(cells) == ["ფართი", "N"]


def test_a_line_written_right_to_left_is_not_touched() -> None:
    cells = [_cell(0, "א", 60.0, 100.0, rtl=True), _cell(1, "ב", 20.0, 100.0, rtl=True)]
    in_line_order(cells)
    assert _read(cells) == ["א", "ב"]


def _texts(cells: list[TextCell]) -> list[str]:
    return [c.text for c in sorted(cells, key=lambda c: c.index)]


def test_a_letters_run_written_across_a_mark_is_mended() -> None:
    """ "ქ თბილისი" one cell and "." another lying inside it: one cell, "ქ. თბილისი"."""
    textlines = [_cell(0, "ქ თბილისი", 20.0, 100.0), _cell(1, ".", 26.0, 100.0)]
    words = [
        _cell(5, "ქ", 20.0, 100.0),
        _cell(6, ".", 26.0, 100.0),
        _cell(7, "თბილისი", 36.0, 100.0),
    ]
    assert _texts(mend_interleaved(textlines, words)) == ["ქ. თბილისი"]


def test_a_mark_that_touches_a_word_is_one_with_it() -> None:
    """A date and its case ending written in two fonts, touching: one cell, so that nothing
    takes the hyphen for a word broken over a line ("01.07.2026-დან", not "01.07.2026დან")."""
    textlines = [
        _cell(0, "01.07.2026-", 20.0, 100.0),
        _cell(1, "დან", 86.0, 100.0),
        _cell(2, "20.07.2026-", 108.0, 100.0),
    ]
    words = [
        _cell(5, "01.07.2026-", 20.0, 100.0),
        _cell(6, "დან", 86.0, 100.0),
        _cell(7, "20.07.2026-", 108.0, 100.0),
    ]
    assert _texts(mend_interleaved(textlines, words)) == ["01.07.2026-დან", "20.07.2026-"]


def test_words_a_space_apart_stay_apart() -> None:
    """Near miss: two runs in two fonts with a space between them are two cells, as written."""
    textlines = [_cell(0, "შპს", 20.0, 100.0), _cell(1, "ABC", 42.0, 100.0)]
    words = [_cell(5, "შპს", 20.0, 100.0), _cell(6, "ABC", 42.0, 100.0)]
    assert _texts(mend_interleaved(textlines, words)) == ["შპს", "ABC"]


def test_pdfs_are_read_by_the_line_ordering_reader() -> None:
    settings = Settings(env="dev")
    options = _build(settings).format_to_options[InputFormat.PDF]
    assert options.backend is LineOrderedPdfBackend

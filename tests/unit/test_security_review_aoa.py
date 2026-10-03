"""The security review of the acceptance-act work: what a hostile upload could make the registry
extract reader, the Word and PDF annotators and the PDF reader do, each closed. Invented content
only; each test fails on the code as it was before its fix.
"""

from __future__ import annotations

import ctypes
import random
import re
import time
import zipfile
from itertools import pairwise
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from docling_core.types.doc.page import BoundingRectangle, TextCell, TextDirection
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from procurement_agent.checks.findings import Finding
from procurement_agent.ingestion import docling_service
from procurement_agent.ingestion.docling_service import mend_interleaved
from procurement_agent.ingestion.text_limits import MAX_SEPARATOR_RUN
from procurement_agent.registry import extract
from procurement_agent.registry.extract import NotAnExtractError, persons, read_extract
from procurement_agent.report import docx_sanitize
from procurement_agent.report.docx_annotate import (
    MAX_PLACES,
    MAX_PLACES_PER_FINDING,
    MAX_SEARCHED,
    UnsupportedDocumentError,
    all_paragraphs,
    annotate_docx,
    paragraphs_in_order,
)
from procurement_agent.report.pdf_annotate import active_names, annotate_pdf
from tests import regex_budget as rb

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEXT_PDF = FIXTURES / "offer_ka_en.pdf"


def _finding(quote: str, **kw: Any) -> Finding:
    return Finding(
        rule_id=kw.pop("rule_id", "AOA.ID_CODES"),
        severity="error",
        message_ka=kw.pop("message_ka", "კოდი არასწორია"),
        message_en=kw.pop("message_en", "the code is wrong"),
        expected=kw.pop("expected", "405000000"),
        actual=quote,
        quote=quote,
        **kw,
    )


def _xml(path: Path, part: str = "word/document.xml") -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(part).decode("utf-8") if part in z.namelist() else ""


# --- 1. the registry extract reader -----------------------------------------------------------

_PRE_FIX_FURNITURE = re.compile(
    r"^[^\n]*http://public\.reestri\.gov\.ge[^\n]*$"
    r"|^[ \t]*(?:გვერდი[ \t]*:?[ \t]*)?\d{1,3}[ \t]*\([ \t]*\d{1,3}[ \t]*\)[ \t]*$",
    re.MULTILINE,
)


class TestTheExtractFooter:
    def test_a_page_word_followed_by_blanks_is_read_in_linear_time(self) -> None:
        """Two runs of blanks around an optional colon were two ways to read the same blanks:
        "გვერდი" and 20,000 blanks took seconds, and a line of them a page, minutes."""
        text = "გვერდი" + " " * 20_000 + "x"
        assert rb._time(extract._PAGE_FURNITURE, text, 1.0) is not None
        assert rb._time(_PRE_FIX_FURNITURE, text, 1.0) is None  # the control: it was slow

    @pytest.mark.parametrize(
        "line", ["გვერდი: 1(2)", "გვერდი 1(2)", "გვერდი : 1 ( 2 )", "  2(6)", "გვერდი:3(3) "]
    )
    def test_a_page_number_line_is_still_taken_away(self, line: str) -> None:
        assert extract._PAGE_FURNITURE.sub("", f"ა\n{line}\nბ") == "ა\n\nბ"

    def test_the_harness_finds_the_pre_fix_pattern_and_passes_the_fixed_one(self) -> None:
        """The harness builds inputs from a pattern's own words now ("გვერდი" + blanks)."""
        assert rb.check_pattern("pre-fix", _PRE_FIX_FURNITURE, budget_s=0.25)
        assert rb.check_pattern("fixed", extract._PAGE_FURNITURE, budget_s=0.25) == []

    def test_no_pattern_of_the_extract_reader_is_slow_on_raw_text(self) -> None:
        """The extract's text reaches these patterns as the file wrote it, line by line."""
        targets = [(n, p) for n, p in rb.discover() if n.startswith(extract.__name__ + ".")]
        assert len(targets) >= 8
        report = rb.scan(targets, budget_s=0.25)
        assert [f.describe() for f in report.findings] == []


class TestTheExtractText:
    def test_a_line_of_blanks_is_cut_and_the_lines_stay_lines(self, tmp_path: Path) -> None:
        doc = Document()
        for text in ("ამონაწერი", " " * 5000 + "x", "y"):
            doc.add_paragraph(text)
        path = tmp_path / "e.docx"
        doc.save(str(path))
        lines = extract.text_of_file(path, "docx").split("\n")
        assert lines[0] == "ამონაწერი" and lines[2] == "y"
        assert len(lines[1]) <= MAX_SEPARATOR_RUN + 1 and lines[1].endswith("x")

    def test_a_word_file_that_is_not_well_formed_is_no_extract(self, tmp_path: Path) -> None:
        """lxml's XMLSyntaxError is a SyntaxError, which nothing caught: a 500, not a note."""
        good = tmp_path / "good.docx"
        Document().save(str(good))
        bad = tmp_path / "bad.docx"
        with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "word/document.xml":
                    data = data[: len(data) // 2]
                dst.writestr(item, data)
        with pytest.raises(NotAnExtractError):
            read_extract(bad, "docx")

    def test_the_persons_read_are_bounded_and_read_in_linear_time(self) -> None:
        block = "\n".join(f"ნიკა ბერიძე{i}, {10_000_000_000 + i}, დირექტორი" for i in range(5000))
        started = time.perf_counter()
        people = persons(block)
        assert time.perf_counter() - started < 5.0
        assert len(people) == extract.MAX_PERSONS


# --- 2. a Word table's cells, each once -------------------------------------------------------


def _table_doc(path: Path, cell_xml: str) -> Path:
    doc = Document()
    doc.add_paragraph("ხელშეკრულება")
    tbl = parse_xml(
        f"<w:tbl {nsdecls('w')}><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/>"
        f"</w:tblGrid><w:tr>{cell_xml}</w:tr></w:tbl>"
    )
    doc.element.body.insert(1, tbl)
    doc.save(str(path))
    return path


def _cell(text: str, props: str = "") -> str:
    return f"<w:tc><w:tcPr>{props}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


class TestTableCells:
    def test_a_merged_cell_is_read_once(self, tmp_path: Path) -> None:
        path = _table_doc(tmp_path / "m.docx", _cell("ს/კ 405123456", '<w:gridSpan w:val="3"/>'))
        doc = Document(str(path))
        assert [p.text for p in all_paragraphs(doc)].count("ს/კ 405123456") == 1
        assert [p.text for p in paragraphs_in_order(doc)].count("ს/კ 405123456") == 1

    def test_a_merged_cell_s_comment_says_its_finding_once(self, tmp_path: Path) -> None:
        """A cell spanning two columns was searched twice: its comment said the finding twice."""
        path = _table_doc(tmp_path / "m.docx", _cell("ს/კ 405123456", '<w:gridSpan w:val="2"/>'))
        out, _ = annotate_docx(path, [_finding("405123456")], tmp_path / "o.docx")
        assert _xml(out, "word/comments.xml").count("the code is wrong") == 1

    def test_a_cell_spanning_millions_of_columns_costs_nothing(self, tmp_path: Path) -> None:
        """python-docx's ``Row.cells`` made a cell object for every column a cell says it spans:
        gigabytes for a 36 KB file saying two billion. (Twenty million here: enough to tell,
        and no more than a mutation run that brings ``Row.cells`` back can afford.)"""
        path = _table_doc(
            tmp_path / "g.docx", _cell("ს/კ 405123456", '<w:gridSpan w:val="20000000"/>')
        )
        started = time.perf_counter()
        annotate_docx(path, [_finding("405123456")], tmp_path / "o.docx")
        assert time.perf_counter() - started < 2.0

    def test_a_first_row_continuing_a_merge_from_above_is_read(self, tmp_path: Path) -> None:
        """python-docx looks for the cell above a continued vertical merge, and failed in the
        first row, where there is none."""
        path = _table_doc(tmp_path / "v.docx", _cell("ს/კ 405123456", "<w:vMerge/>"))
        out, unanchored = annotate_docx(path, [_finding("405123456")], tmp_path / "o.docx")
        assert unanchored == [] and "commentRangeStart" in _xml(out)


# --- 3. as many places as a file repeats a value ----------------------------------------------


def _repeated(path: Path, value: str, times: int, *, runs: bool = False) -> Path:
    doc = Document()
    paragraph = doc.add_paragraph()
    if runs:
        for _ in range(times):
            paragraph.add_run(f"{value} ")
    else:
        paragraph.add_run(f"{value} " * times)
    doc.add_paragraph("სხვა ტექსტი")
    doc.save(str(path))
    return path


class TestManyPlaces:
    def test_a_value_repeated_thousands_of_times_is_marked_in_bounded_time(
        self, tmp_path: Path
    ) -> None:
        path = _repeated(tmp_path / "r.docx", "405123456", 3000)
        finding = _finding("405123456", replacement="405000000")
        started = time.perf_counter()
        out, unanchored = annotate_docx(path, [finding], tmp_path / "o.docx")
        assert time.perf_counter() - started < 15.0
        assert unanchored == []
        xml = _xml(out)
        assert xml.count("<w:commentRangeStart ") == MAX_PLACES_PER_FINDING + 1  # + the note
        # a finding cut short gets no tracked change: accepting all must not half-correct it
        assert "<w:ins " not in xml
        assert f"more than {MAX_SEARCHED}" in _xml(out, "word/comments.xml")

    def test_a_value_in_many_runs_is_marked_in_bounded_time(self, tmp_path: Path) -> None:
        path = _repeated(tmp_path / "r.docx", "405123456", 2000, runs=True)
        started = time.perf_counter()
        out, _ = annotate_docx(path, [_finding("405123456")], tmp_path / "o.docx")
        assert time.perf_counter() - started < 15.0
        assert f"marked at {MAX_PLACES_PER_FINDING} of its" in _xml(out, "word/comments.xml")

    def test_a_value_written_a_few_times_is_marked_and_corrected_everywhere(
        self, tmp_path: Path
    ) -> None:
        path = _repeated(tmp_path / "r.docx", "405123456", 4)
        finding = _finding("405123456", replacement="405000000")
        out, _ = annotate_docx(path, [finding], tmp_path / "o.docx")
        xml = _xml(out)
        assert xml.count("<w:commentRangeStart ") == 4
        assert xml.count("<w:ins ") == 4
        assert "marked at" not in _xml(out, "word/comments.xml")

    def test_the_file_s_limit_holds_across_findings(self, tmp_path: Path) -> None:
        doc = Document()
        values = [f"40512{k:04d}" for k in range(8)]
        for value in values:
            doc.add_paragraph(f"{value} " * 60)
        path = tmp_path / "r.docx"
        doc.save(str(path))
        out, _ = annotate_docx(path, [_finding(v) for v in values], tmp_path / "o.docx")
        assert _xml(out).count("<w:commentRangeStart ") <= MAX_PLACES + 1

    def test_a_paragraph_s_places_cost_no_more_each_the_more_there_are(
        self, tmp_path: Path
    ) -> None:
        """Every place was searched for from the start of its paragraph's text, and its runs
        read again: the cost of a place grew with the number of places before it."""

        def cost(times: int) -> float:
            path = _repeated(tmp_path / f"c{times}.docx", "7", times, runs=True)
            started = time.perf_counter()
            annotate_docx(path, [_finding("7")], tmp_path / "o.docx")
            return (time.perf_counter() - started) / times

        small, large = cost(100), cost(1600)
        # sixteen times the places at four times the cost a place was the old search; a linear
        # one stays near one, and six leaves room for a loaded machine
        assert large < max(small, 0.001) * 6


# --- 4. text XML cannot carry -----------------------------------------------------------------

_BAD = "ა\ud800ბ\ufffeგ\uffffდ\x0b"


class TestCharactersXmlCannotCarry:
    def test_a_word_comment_and_a_tracked_change_drop_them(self, tmp_path: Path) -> None:
        path = _repeated(tmp_path / "r.docx", "405123456", 1)
        finding = _finding("405123456", message_ka=_BAD, expected=_BAD, replacement=f"405{_BAD}")
        out, _ = annotate_docx(path, [finding], tmp_path / "o.docx")
        assert "აბგდ" in _xml(out, "word/comments.xml")
        Document(str(out))  # well-formed

    def test_a_pdf_note_drops_them(self, tmp_path: Path) -> None:
        finding = _finding("405123456", message_ka=_BAD, expected=_BAD)
        out, _ = annotate_pdf(TEXT_PDF, [finding], tmp_path / "o.pdf")
        assert out.exists()


# --- 5. what an annotated PDF carries ---------------------------------------------------------

_SCRIPT = b"<< /S /JavaScript /JS (app.alert\\('invented'\\);) >>"
_FILESPEC = b"<< /Type /Filespec /F (invented.exe) /UF (invented.exe) /EF << /F 981 0 R >> >>"
_EMBEDDED = b"<< /Type /EmbeddedFile /Length 11 >>\nstream\nMZINVENTED!\nendstream"


def _updated(out: Path, page: bytes, extra: dict[int, bytes]) -> Path:
    """The text fixture with its first page's dictionary replaced by ``page`` and ``extra``
    objects added, by an incremental update."""
    data = TEXT_PDF.read_bytes()
    root = int(re.findall(rb"/Root (\d+) 0 R", data)[-1])
    catalog = re.search(rb"\n%d 0 obj\s*(<<.*?>>)\s*endobj" % root, data, re.S)
    assert catalog is not None
    pages_ref = re.search(rb"/Pages (\d+) 0 R", catalog.group(1))
    assert pages_ref is not None
    pages = re.search(rb"\n%s 0 obj\s*(<<.*?>>)\s*endobj" % pages_ref.group(1), data, re.S)
    assert pages is not None
    first = re.search(rb"/Kids\s*\[\s*(\d+) 0 R", pages.group(1))
    assert first is not None
    objs = {int(first.group(1)): page, **extra}
    size = int(re.findall(rb"/Size (\d+)", data)[-1])
    prev = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    body = bytearray(data if data.endswith(b"\n") else data + b"\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(body)
        body += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n" + b"".join(b"%d 1\n%010d 00000 n \n" % (n, offsets[n]) for n in sorted(objs))
    body += b"trailer\n<< /Size %d /Root %d 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (
        max(size, 1000),
        root,
        prev,
        xref,
    )
    out.write_bytes(bytes(body))
    return out


def _page(extra: bytes = b"", resources: bytes = b"", contents: bytes = b"15 0 R") -> bytes:
    return (
        b"<< /Type /Page /Parent 14 0 R /MediaBox [0 0 595.2756 841.8898] /Contents "
        + contents
        + b" /Resources << /Font 1 0 R /ProcSet [/PDF /Text] "
        + resources
        + b" >>"
        + extra
        + b" >>"
    )


def _assert_carries_nothing(out: Path) -> None:
    data = out.read_bytes()
    assert active_names(data) == set()
    assert b"INVENTED" not in data and b"invented" not in data


_METADATA = b"<< /Type /Metadata /Subtype /XML /Length 8 >>\nstream\nINVENTED\nendstream"


class TestWhatTheAnnotatedPdfCarries:
    @pytest.mark.parametrize(
        ("page", "extra"),
        [
            (_page(b" /AF [980 0 R]"), {980: _FILESPEC, 981: _EMBEDDED}),
            (
                _page(b" /Annots 990 0 R"),
                {990: b"<< /Type /Annot /Subtype /Link /Rect [9 9 99 29] /A " + _SCRIPT + b" >>"},
            ),
            (_page(b" /AA [ " + _SCRIPT + b" ]"), {}),
            (
                _page(b" /Metadata 996 0 R /PieceInfo << /X << /Data (invented) >> >>"),
                {996: _METADATA},
            ),
        ],
        ids=["associated-file", "annots-not-an-array", "actions-as-an-array", "metadata"],
    )
    def test_what_a_page_names_stays_behind(
        self, tmp_path: Path, page: bytes, extra: dict[int, bytes]
    ) -> None:
        """Importing pages copied every entry of a page's dictionary; each page is drawn anew."""
        src = _updated(tmp_path / "act.pdf", page, extra)
        out, _ = annotate_pdf(src, [_finding("405123456")], tmp_path / "o.pdf")
        _assert_carries_nothing(out)

    def test_a_clean_act_s_copy_carries_nothing_either(self, tmp_path: Path) -> None:
        page = _page(b" /AF [980 0 R]")
        src = _updated(tmp_path / "act.pdf", page, {980: _FILESPEC, 981: _EMBEDDED})
        out, _ = annotate_pdf(src, [], tmp_path / "o.pdf")
        _assert_carries_nothing(out)

    def test_a_form_its_content_draws_with_a_file_of_its_own_is_refused(
        self, tmp_path: Path
    ) -> None:
        """What a page's resources hold is the page's content, drawn with it: the check of the
        saved copy is what keeps a file attached there from going out."""
        form = (
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /AF [980 0 R] /Length 8 >>"
            b"\nstream\n0 0 m S \nendstream"
        )
        draw = b"<< /Length 12 >>\nstream\nq /Inv Do Q \nendstream"
        page = _page(resources=b"/XObject << /Inv 982 0 R >>", contents=b"[15 0 R 983 0 R]")
        src = _updated(
            tmp_path / "act.pdf", page, {980: _FILESPEC, 981: _EMBEDDED, 982: form, 983: draw}
        )
        with pytest.raises(UnsupportedDocumentError, match="attached file"):
            annotate_pdf(src, [_finding("405123456")], tmp_path / "o.pdf")
        assert not (tmp_path / "o.pdf").exists()

    @pytest.mark.parametrize("turn", [0, 90, 180, 270])
    def test_a_page_is_drawn_where_it_was(self, tmp_path: Path, turn: int) -> None:
        """The redrawn page shows what the page showed, pixel for pixel, cropped and turned."""
        src = tmp_path / "act.pdf"
        pdf = pdfium.PdfDocument(str(TEXT_PDF))
        try:
            pdf[0].set_cropbox(40.0, 60.0, 500.0, 760.0)
            pdf[0].set_rotation(turn)
            pdf.save(str(src))
        finally:
            pdf.close()
        out, _ = annotate_pdf(src, [], tmp_path / "o.pdf")
        a, b = pdfium.PdfDocument(str(src)), pdfium.PdfDocument(str(out))
        try:
            assert a[0].render().to_pil().tobytes() == b[0].render().to_pil().tobytes()
        finally:
            a.close()
            b.close()


class TestActiveNames:
    @pytest.mark.parametrize(
        ("data", "found"),
        [
            (b"<< /S /JavaScript /JS (x) >>", {"JavaScript", "JS"}),
            (b"<< /J#53 (x) >>", {"JS"}),  # an escaped name is the name
            (b"<< /AF [1 0 R] >>", {"AF"}),
            (b"<< /BaseFont /AAAAAB+Sans /Contents (see /JS (here)) >>", set()),
            (b"<< /Length 9 >>\nstream\n/AA /JS x\nendstream\n<< /Type /Page >>", set()),
            (b"<< /T <2F4A53> >>", set()),  # a hex string
            (b"% /JS in a comment\n<< /Type /Annot >>", set()),
        ],
    )
    def test_names_are_read_as_pdf_reads_them(self, data: bytes, found: set[str]) -> None:
        assert active_names(data) == found


def _pdf_with(text: str, path: Path) -> Path:
    """A one-page PDF whose text layer is ``text`` on one line, in a standard font."""
    pdf = pdfium.PdfDocument.new()
    try:
        page = pdf.new_page(20_000.0, 842.0)
        obj = pdfium_c.FPDFPageObj_NewTextObj(pdf, b"Helvetica", 10.0)
        data = (text + "\x00").encode("utf-16-le")
        buffer = (ctypes.c_ushort * (len(data) // 2)).from_buffer_copy(data)
        pdfium_c.FPDFText_SetText(obj, buffer)
        pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 10, 400)
        pdfium_c.FPDFPage_InsertObject(page, obj)
        page.gen_content()
        pdf.save(str(path))
    finally:
        pdf.close()
    return path


class TestManyPdfPlaces:
    def test_a_value_repeated_hundreds_of_times_is_marked_up_to_the_limit(
        self, tmp_path: Path
    ) -> None:
        src = _pdf_with("405123456 " * 400, tmp_path / "r.pdf")
        started = time.perf_counter()
        out, unanchored = annotate_pdf(src, [_finding("405123456")], tmp_path / "o.pdf")
        assert time.perf_counter() - started < 15.0
        assert unanchored == []
        pdf = pdfium.PdfDocument(str(out))
        try:
            page = pdf[0]
            subtypes = []
            for i in range(pdfium_c.FPDFPage_GetAnnotCount(page)):
                annot = pdfium_c.FPDFPage_GetAnnot(page, i)
                subtypes.append(pdfium_c.FPDFAnnot_GetSubtype(annot))
                pdfium_c.FPDFPage_CloseAnnot(annot)
        finally:
            pdf.close()
        assert subtypes.count(pdfium_c.FPDF_ANNOT_HIGHLIGHT) == MAX_PLACES_PER_FINDING
        assert subtypes.count(pdfium_c.FPDF_ANNOT_TEXT) == 1  # the note saying so


# --- 6. Word fields that link out -------------------------------------------------------------


def _with_field(path: Path, instruction: str, *, simple: bool) -> Path:
    doc = Document()
    paragraph = doc.add_paragraph("ხელშეკრულება ")
    w = nsdecls("w")
    if simple:
        paragraph._p.append(
            parse_xml(
                f'<w:fldSimple {w} w:instr="{instruction}"><w:r><w:t>ნახვა</w:t></w:r>'
                "</w:fldSimple>"
            )
        )
    else:
        for xml in (
            f'<w:r {w}><w:fldChar w:fldCharType="begin"/></w:r>',
            f'<w:r {w}><w:instrText xml:space="preserve">{instruction}</w:instrText></w:r>',
            f'<w:r {w}><w:fldChar w:fldCharType="separate"/></w:r>',
            f"<w:r {w}><w:t>ნახვა</w:t></w:r>",
            f'<w:r {w}><w:fldChar w:fldCharType="end"/></w:r>',
        ):
            paragraph._p.append(parse_xml(xml))
    doc.save(str(path))
    return path


class TestFieldsThatLinkOut:
    @pytest.mark.parametrize("simple", [True, False], ids=["simple", "complex"])
    @pytest.mark.parametrize(
        ("instruction", "kept"),
        [
            (" HYPERLINK &quot;https://example.com/a&quot; ", True),
            (" HYPERLINK \\l &quot;_Toc1&quot; ", True),
            (" HYPERLINK &quot;mailto:a@example.com&quot; ", True),
            (" HYPERLINK &quot;file:///C:/x.exe&quot; ", False),
            (" HYPERLINK &quot;\\\\\\\\host\\\\share\\\\x&quot; ", False),
            (" HYPERLINK &quot;ms-msdt:/id x&quot; ", False),
            (" HYPERLINK &quot;javascript:alert(1)&quot; ", False),
            (" &quot;INCLUDETEXT&quot; &quot;x.docx&quot; ", False),
            (" RD &quot;x.docx&quot; ", False),
            (" PRINT x ", False),
        ],
    )
    def test_a_field_links_where_a_link_may(
        self, tmp_path: Path, instruction: str, kept: bool, simple: bool
    ) -> None:
        src = _with_field(tmp_path / "f.docx", instruction, simple=simple)
        out, _ = annotate_docx(src, [], tmp_path / "o.docx")
        xml = _xml(out)
        word = instruction.replace("&quot;", "").split()[0]
        field = re.search(rf'(?:w:instr="|<w:instrText[^>]*>)[^"<]*\b{word}\b', xml)
        assert (field is not None) is kept
        assert "ნახვა" in xml  # the text the field showed stays

    def test_the_relationship_policy_and_the_field_policy_are_one(self) -> None:
        for target in ("https://a.example", "mailto:a@b.example", "file:///x", "\\\\h\\s", "c:\\x"):
            field_target = target.replace("\\", "\\\\")
            assert docx_sanitize._safe_link(target) is docx_sanitize._safe_field_link(
                f'"{field_target}"'
            )


# --- 7. mending a PDF page's lines ------------------------------------------------------------


def _text_cell(index: int, text: str, left: float, top: float, width: float) -> TextCell:
    return TextCell(
        index=index,
        rect=BoundingRectangle(
            r_x0=left,
            r_y0=top,
            r_x1=left + width,
            r_y1=top,
            r_x2=left + width,
            r_y2=top + 12.0,
            r_x3=left,
            r_y3=top + 12.0,
            coord_origin=CoordOrigin.TOPLEFT,
        ),
        text=text,
        orig=text,
        text_direction=TextDirection.LEFT_TO_RIGHT,
        from_ocr=False,
    )


def _rebuilt_as_it_was(textlines: list[Any], clump: list[int], first: int, words: list[Any]) -> Any:
    """``docling_service._rebuilt`` before the review: every word read for every clump."""
    boxes = [textlines[k].rect.to_bounding_box() for k in clump]
    left, right = min(b.l for b in boxes), max(b.r for b in boxes)
    top, bottom = min(min(b.t, b.b) for b in boxes), max(max(b.t, b.b) for b in boxes)
    inside = []
    for word in words:
        b = word.rect.to_bounding_box()
        x, y = (b.l + b.r) / 2, (b.t + b.b) / 2
        if left <= x <= right and top <= y <= bottom and word.text.strip():
            inside.append((b, word.text.strip()))
    inside.sort(key=lambda w: w[0].l)
    if inside:
        text = inside[0][1]
        for (before, _), (after, word) in pairwise(inside):
            gap = after.l - before.r
            text += (" " if gap > docling_service._SPACE_GAP * (bottom - top) else "") + word
    else:
        text = " ".join(
            textlines[k].text.strip()
            for k in sorted(clump, key=lambda k: textlines[k].rect.to_bounding_box().l)
        )
    cell = textlines[first].model_copy(deep=True)
    cell.text = cell.orig = text
    cell.rect = BoundingRectangle.from_bounding_box(
        BoundingBox(l=left, t=top, r=right, b=bottom, coord_origin=CoordOrigin.TOPLEFT)
    )
    return cell


def _random_page(rng: random.Random) -> tuple[list[TextCell], list[TextCell]]:
    cells: list[TextCell] = []
    words: list[TextCell] = []
    for line in range(rng.randint(1, 12)):
        top = 20.0 * line + rng.choice([0.0, 0.5, 3.0])
        x = 10.0
        for _ in range(rng.randint(1, 6)):
            width = rng.choice([6.0, 12.0, 30.0])
            x += rng.choice([-8.0, -2.0, 0.0, 1.0, 5.0, 20.0])  # across, touching or apart
            cells.append(_text_cell(len(cells), rng.choice(["ა", "12", ".", "ბგ"]), x, top, width))
            for _ in range(rng.randint(0, 3)):
                left = x + rng.choice([0.0, 2.0, width / 2, width - 1.0])
                words.append(
                    _text_cell(len(words), rng.choice(["ა", "1", ".", " "]), left, top, 4.0)
                )
            x += width
    rng.shuffle(cells)
    return cells, words


class TestMending:
    def test_the_words_are_found_as_before_on_random_pages(self, monkeypatch: Any) -> None:
        """The words inside a clump are found by bisection now: on 400 random pages - cells
        across each other, touching and apart; words at the same left - the mended cells are the
        ones the old search made."""
        rng = random.Random(20261003)
        for _ in range(400):
            cells, words = _random_page(rng)
            new = mend_interleaved([c.model_copy(deep=True) for c in cells], words)
            with monkeypatch.context() as m:
                m.setattr(
                    docling_service,
                    "_rebuilt",
                    lambda t, c, f, _w, words=words: _rebuilt_as_it_was(t, c, f, words),
                )
                old = mend_interleaved([c.model_copy(deep=True) for c in cells], words)
            assert [(c.index, c.text, c.rect) for c in new] == [
                (c.index, c.text, c.rect) for c in old
            ]

    def test_a_page_of_many_clumps_is_mended_in_bounded_time(self) -> None:
        """Every word of the page was read for every clump: a 16 KB page took 20 seconds."""
        cells: list[TextCell] = []
        words: list[TextCell] = []
        for line in range(60):
            for k in range(30):
                left = 10.0 + 20.0 * k  # a clump, then a space
                cells.append(_text_cell(len(cells), "ა", left, 20.0 * line, 10.0))
                cells.append(_text_cell(len(cells), ".", left + 9.0, 20.0 * line, 4.0))
                words.append(_text_cell(len(words), "ა.", left, 20.0 * line, 13.0))
        started = time.perf_counter()
        mended = mend_interleaved(cells, words)
        assert time.perf_counter() - started < 5.0
        assert len(mended) == 1800 and all(c.text == "ა." for c in mended)

    def test_a_page_past_the_limits_is_left_as_read(self) -> None:
        cells = [
            _text_cell(i, "ა", 10.0 + (i % 50) * 5.0, 20.0 * (i // 50), 6.0)
            for i in range(docling_service.MEND_MAX_CELLS + 1)
        ]
        assert mend_interleaved(cells, []) is cells

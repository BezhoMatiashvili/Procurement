"""Findings written into a PDF act: a highlight and a note on the words at fault, as the client
asks for an act that arrives as a PDF. The PDFs are the synthetic fixtures; the findings invented.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from procurement_agent.checks.findings import Finding
from procurement_agent.report.docx_annotate import UnsupportedDocumentError
from procurement_agent.report.pdf_annotate import PdfItem, annotate_pdf, docling_items

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEXT_PDF = FIXTURES / "offer_ka_en.pdf"
SCANNED_PDF = FIXTURES / "offer_ka_en_scanned.pdf"
HIGHLIGHT = pdfium_c.FPDF_ANNOT_HIGHLIGHT


def _finding(quote: str, severity: str = "error", rule: str = "AOA.ID_CODES") -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,  # type: ignore[arg-type]
        message_ka="შეცდომა",
        message_en="error",
        expected="000000000",
        actual=quote,
        quote=quote,
    )


def _annots(path: Path) -> list[dict[str, Any]]:
    """Every annotation of every page: its page, subtype, quads, rectangle, note and colour."""
    out: list[dict[str, Any]] = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            for k in range(pdfium_c.FPDFPage_GetAnnotCount(page)):
                a = pdfium_c.FPDFPage_GetAnnot(page, k)
                size = pdfium_c.FPDFAnnot_GetStringValue(a, b"Contents", None, 0)
                buf = ctypes.create_string_buffer(size)
                pdfium_c.FPDFAnnot_GetStringValue(
                    a, b"Contents", ctypes.cast(buf, ctypes.POINTER(pdfium_c.FPDF_WCHAR)), size
                )
                rect = pdfium_c.FS_RECTF()
                pdfium_c.FPDFAnnot_GetRect(a, ctypes.byref(rect))
                rgba = [ctypes.c_uint() for _ in range(4)]
                pdfium_c.FPDFAnnot_GetColor(
                    a, pdfium_c.FPDFANNOT_COLORTYPE_Color, *(ctypes.byref(c) for c in rgba)
                )
                out.append(
                    {
                        "page": i,
                        "subtype": pdfium_c.FPDFAnnot_GetSubtype(a),
                        "quads": pdfium_c.FPDFAnnot_CountAttachmentPoints(a),
                        "rect": (rect.left, rect.bottom, rect.right, rect.top),
                        "note": buf.raw.decode("utf-16-le").rstrip("\x00"),
                        "colour": tuple(c.value for c in rgba[:3]),
                    }
                )
                pdfium_c.FPDFPage_CloseAnnot(a)
    finally:
        pdf.close()
    return out


def _word_box(path: Path, word: str, nth: int = 0) -> tuple[float, float, float, float]:
    """Where PDFium itself finds ``word`` on the first page."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        textpage = pdf[0].get_textpage()
        searcher = textpage.search(word, match_case=True)
        for _ in range(nth):
            searcher.get_next()
        start, count = searcher.get_next()
        boxes = [textpage.get_charbox(i, loose=False) for i in range(start, start + count)]
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
    finally:
        pdf.close()


def _close(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return all(abs(x - y) < 0.5 for x, y in zip(a, b, strict=True))


class TestTextLayer:
    def test_a_code_is_highlighted_wherever_the_act_writes_it(self, tmp_path: Path) -> None:
        out, unanchored = annotate_pdf(TEXT_PDF, [_finding("405123456")], tmp_path / "o.pdf")
        assert unanchored == []
        marks = [a for a in _annots(out) if a["subtype"] == HIGHLIGHT]
        assert len(marks) == 2  # the Georgian line and the English one
        assert _close(marks[0]["rect"], _word_box(TEXT_PDF, "405123456"))
        assert _close(marks[1]["rect"], _word_box(TEXT_PDF, "405123456", nth=1))
        assert "AOA.ID_CODES" in marks[0]["note"] and "000000000" in marks[0]["note"]

    def test_a_name_broken_across_two_lines_is_one_note_with_a_quad_a_line(
        self, tmp_path: Path
    ) -> None:
        out, _ = annotate_pdf(
            TEXT_PDF, [_finding("ნინო კაპანაძის", rule="AOA.DIRECTOR")], tmp_path / "o.pdf"
        )
        [mark] = [a for a in _annots(out) if a["subtype"] == HIGHLIGHT]
        assert mark["quads"] == 2

    def test_quotes_the_text_layer_writes_otherwise_do_not_hide_a_name(
        self, tmp_path: Path
    ) -> None:
        """The act writes ASCII quotes, the finding Georgian ones: the quotes are set aside."""
        out, unanchored = annotate_pdf(
            TEXT_PDF, [_finding("შპს „სინთეზ ენერჯი“", rule="AOA.PARTY_NAME")], tmp_path / "o.pdf"
        )
        assert unanchored == []
        assert len([a for a in _annots(out) if a["subtype"] == HIGHLIGHT]) == 1

    def test_findings_on_the_same_words_share_a_note_in_the_gravest_colour(
        self, tmp_path: Path
    ) -> None:
        findings = [
            _finding("405123456", severity="warning", rule="AOA.REGISTRY"),
            _finding("405123456", severity="error"),
        ]
        out, _ = annotate_pdf(TEXT_PDF, findings, tmp_path / "o.pdf")
        marks = [a for a in _annots(out) if a["subtype"] == HIGHLIGHT]
        assert len(marks) == 2
        assert all("AOA.REGISTRY" in m["note"] and "AOA.ID_CODES" in m["note"] for m in marks)
        assert {m["colour"] for m in marks} == {(230, 70, 60)}

    def test_the_summary_and_what_stands_nowhere_go_in_a_note_on_page_one(
        self, tmp_path: Path
    ) -> None:
        orphan = _finding("სიტყვები, რომლებიც აქ არ წერია", rule="AOA.PARTY_MISSING")
        out, unanchored = annotate_pdf(
            TEXT_PDF, [orphan], tmp_path / "o.pdf", summary="[შეჯამება / Summary] test"
        )
        assert [f.rule_id for f in unanchored] == ["AOA.PARTY_MISSING"]
        [note] = _annots(out)
        assert note["subtype"] == pdfium_c.FPDF_ANNOT_TEXT and note["page"] == 0
        assert "Summary" in note["note"] and "AOA.PARTY_MISSING" in note["note"]

    def test_nothing_but_annotations_is_added(self, tmp_path: Path) -> None:
        """No script, action or link reaches the file a reviewer opens."""
        out, _ = annotate_pdf(TEXT_PDF, [_finding("405123456")], tmp_path / "o.pdf")
        data = out.read_bytes()
        for marker in (b"/JavaScript", b"/JS ", b"/OpenAction", b"/Launch", b"/URI", b"/AA "):
            assert marker not in data
        assert TEXT_PDF.read_bytes() != data


class TestScanned:
    def test_a_scanned_page_is_marked_by_the_line_that_holds_the_words(
        self, tmp_path: Path
    ) -> None:
        line = PdfItem(1, (72.0, 700.0, 400.0, 712.0), 'შპს "სინთეზ ენერჯი" (ს/კ 405123456)')
        out, unanchored = annotate_pdf(
            SCANNED_PDF, [_finding("405123456")], tmp_path / "o.pdf", items=[line]
        )
        assert unanchored == []
        [mark] = _annots(out)
        assert mark["subtype"] == HIGHLIGHT
        assert _close(mark["rect"], (72.0, 700.0, 400.0, 712.0))
        assert "scanned page" in mark["note"]

    def test_docling_boxes_are_turned_to_the_pdfs_origin(self) -> None:
        from docling_core.types.doc import (
            BoundingBox,
            CoordOrigin,
            DocItemLabel,
            DoclingDocument,
            ProvenanceItem,
            Size,
        )

        doc = DoclingDocument(name="x")
        doc.add_page(page_no=1, size=Size(width=600, height=800))
        box = BoundingBox(l=72, t=100, r=300, b=112, coord_origin=CoordOrigin.TOPLEFT)
        doc.add_text(
            label=DocItemLabel.TEXT,
            text="ს/კ 405123456",
            prov=ProvenanceItem(page_no=1, bbox=box, charspan=(0, 13)),
        )
        [item] = docling_items(doc)
        assert item.page == 1 and item.text == "ს/კ 405123456"
        assert _close(item.box, (72.0, 688.0, 300.0, 700.0))


def _with_active_content(src: Path, out: Path, *, page_action: bool = False) -> Path:
    """``src`` with what a PDF can run or carry added by an incremental update: an action and a
    script run on opening, an attached file, and on the first page a link that launches a
    program and one that runs a script - and, with ``page_action``, a script run when the first
    page opens. Invented content; no viewer runs it here."""
    import re

    data = src.read_bytes()
    root = int(re.findall(rb"/Root (\d+) 0 R", data)[-1])

    def obj(n: int) -> bytes:
        found = re.search(rb"\n%d 0 obj\s*(<<.*?>>)\s*endobj" % n, data, re.S)
        assert found is not None
        return found.group(1)

    catalog = obj(root)
    pages = obj(int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1)))  # type: ignore[union-attr]
    first = int(re.search(rb"/Kids\s*\[\s*(\d+) 0 R", pages).group(1))  # type: ignore[union-attr]
    script = b"<< /S /JavaScript /JS (app.alert\\('invented'\\);) >>"
    launch = b"<< /S /Launch /F (x.exe) >>"
    objs = {
        990: script,
        991: b"<< /Names [(opened) 992 0 R] >>",
        992: script,
        993: b"<< /Type /Filespec /F (payload.exe) /EF << /F 994 0 R >> >>",
        994: b"<< /Type /EmbeddedFile /Length 5 >>\nstream\nMZ...\nendstream",
        995: b"<< /Names [(payload.exe) 993 0 R] >>",
        996: b"<< /Type /Annot /Subtype /Link /Rect [10 10 100 30] /A " + launch + b" >>",
        997: b"<< /Type /Annot /Subtype /Link /Rect [10 40 100 60] /A " + script + b" >>",
        root: catalog[:-2]
        + b" /OpenAction 990 0 R /Names << /JavaScript 991 0 R /EmbeddedFiles 995 0 R >> >>",
        first: obj(first)[:-2]
        + (b" /AA << /O " + script + b" >>" if page_action else b"")
        + b" /Annots [996 0 R 997 0 R] >>",
    }
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
        max(size, 998),
        root,
        prev,
        xref,
    )
    out.write_bytes(bytes(body))
    return out


_ACTIVE = (b"/JavaScript", b"/OpenAction", b"/EmbeddedFile", b"/Launch", b"app.alert", b"payload")


class TestActiveContent:
    """Red team R6, R6-08: the annotated copy is the act's pages and the notes, nothing that runs
    or is carried inside - a reviewer opens it as the application's own output."""

    def test_what_a_pdf_runs_or_carries_does_not_reach_the_annotated_copy(
        self, tmp_path: Path
    ) -> None:
        src = _with_active_content(TEXT_PDF, tmp_path / "act.pdf")
        assert all(m in src.read_bytes() for m in _ACTIVE)
        out, unanchored = annotate_pdf(src, [_finding("405123456")], tmp_path / "o.pdf")
        data = out.read_bytes()
        assert [m for m in _ACTIVE if m in data] == []
        pdf = pdfium.PdfDocument(str(out))
        try:
            assert pdfium_c.FPDFDoc_GetJavaScriptActionCount(pdf) == 0
            assert pdfium_c.FPDFDoc_GetAttachmentCount(pdf) == 0
        finally:
            pdf.close()
        # the words are still marked where they stand, and nothing else is there
        assert unanchored == []
        marks = _annots(out)
        assert [m["subtype"] for m in marks] == [HIGHLIGHT, HIGHLIGHT]
        assert _close(marks[0]["rect"], _word_box(TEXT_PDF, "405123456"))

    def test_a_page_that_runs_a_script_when_opened_is_refused(self, tmp_path: Path) -> None:
        src = _with_active_content(TEXT_PDF, tmp_path / "act.pdf", page_action=True)
        with pytest.raises(UnsupportedDocumentError):
            annotate_pdf(src, [_finding("405123456")], tmp_path / "o.pdf")


def _cropped(src: Path, out: Path, crop: tuple[float, ...], turn: int) -> Path:
    pdf = pdfium.PdfDocument(str(src))
    try:
        page = pdf[0]
        page.set_cropbox(*crop)
        page.set_rotation(turn)
        pdf.save(str(out))
    finally:
        pdf.close()
    return out


def _shown(path: Path, box: tuple[float, ...]) -> tuple[float, float, float, float]:
    """A box on the first page in the page's own space, as the page is shown: PDFium's own
    transform, its origin the shown page's bottom left."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[0]
        w, h = page.get_size()
        xs, ys = [], []
        for x, y in ((box[0], box[1]), (box[2], box[3])):
            dx, dy = ctypes.c_int(), ctypes.c_int()
            pdfium_c.FPDF_PageToDevice(
                page, 0, 0, int(w * 10), int(h * 10), 0, x, y, ctypes.byref(dx), ctypes.byref(dy)
            )
            xs.append(dx.value / 10)
            ys.append(h - dy.value / 10)
        return (min(xs), min(ys), max(xs), max(ys))
    finally:
        pdf.close()


_CROP = (40.0, 60.0, 500.0, 760.0)


class TestAPageCroppedAndTurned:
    """Red team R6, R6-13 and R6-14: a page shows its crop box, turned as it says - a note or a
    mark placed by the page's full size, unturned, falls outside what is shown or on the wrong
    line."""

    @pytest.mark.parametrize("turn", [0, 90, 180, 270])
    def test_the_note_is_at_the_top_left_corner_the_page_shows(
        self, tmp_path: Path, turn: int
    ) -> None:
        src = _cropped(TEXT_PDF, tmp_path / "act.pdf", _CROP, turn)
        out, _ = annotate_pdf(src, [], tmp_path / "o.pdf", summary="[შეჯამება / Summary] test")
        [note] = _annots(out)
        left, bottom, right, top = note["rect"]
        assert _CROP[0] <= left < right <= _CROP[2] and _CROP[1] <= bottom < top <= _CROP[3]
        shown = _shown(out, note["rect"])
        pdf = pdfium.PdfDocument(str(out))
        try:
            _, height = pdf[0].get_size()
        finally:
            pdf.close()
        assert _close(shown, (12.0, height - 32.0, 32.0, height - 12.0))

    @pytest.mark.parametrize("turn", [0, 90, 180, 270])
    def test_a_scanned_line_is_marked_where_the_page_shows_it(
        self, tmp_path: Path, turn: int
    ) -> None:
        src = _cropped(SCANNED_PDF, tmp_path / "scan.pdf", _CROP, turn)
        # a line as Docling gives it: on the page as shown, its origin the shown bottom left
        line = PdfItem(1, (72.0, 300.0, 400.0, 312.0), 'შპს "სინთეზ ენერჯი" (ს/კ 405123456)')
        out, unanchored = annotate_pdf(
            src, [_finding("405123456")], tmp_path / "o.pdf", items=[line]
        )
        assert unanchored == []
        [mark] = _annots(out)
        assert _close(_shown(out, mark["rect"]), line.box)


class TestRefused:
    def test_a_file_that_is_not_a_pdf_is_refused(self, tmp_path: Path) -> None:
        other = tmp_path / "act.docx"
        other.write_bytes(b"PK")
        with pytest.raises(UnsupportedDocumentError):
            annotate_pdf(other, [], tmp_path / "o.pdf")

    def test_a_broken_pdf_is_refused(self, tmp_path: Path) -> None:
        broken = tmp_path / "act.pdf"
        broken.write_bytes(b"%PDF-1.7 not a pdf")
        with pytest.raises(UnsupportedDocumentError):
            annotate_pdf(broken, [], tmp_path / "o.pdf")

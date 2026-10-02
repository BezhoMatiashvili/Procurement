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

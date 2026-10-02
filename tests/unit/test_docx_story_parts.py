"""Findings marked where a Word file keeps text outside its body's paragraphs and tables: a form's
content controls (``w:sdt``) and text boxes - text Docling reads and the check reports on, which
was marked only in the note at the top. Invented text only.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from lxml import etree

from procurement_agent.checks.findings import Finding
from procurement_agent.eval.score import comment_anchors
from procurement_agent.report.docx_annotate import annotate_docx, paragraphs_in_order

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_VML = "urn:schemas-microsoft-com:vml"


def _finding(quote: str, replacement: str | None = "987654321") -> Finding:
    return Finding(
        rule_id="AOA.ID_CODES",
        severity="error",
        message_ka="შეცდომა",
        message_en="error",
        expected=replacement,
        actual=quote,
        quote=quote,
        replacement=replacement,
    )


def _content_control(text: str) -> object:
    return parse_xml(
        f"<w:sdt {nsdecls('w')}><w:sdtPr/><w:sdtContent>"
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:sdtContent></w:sdt>"
    )


def _vml_box(text: str) -> object:
    return parse_xml(
        f'<w:r {nsdecls("w")} xmlns:v="{_VML}"><w:pict><v:shape><v:textbox><w:txbxContent>'
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
    )


def _both_boxes(text: str) -> object:
    """A text box as Word writes it: DrawingML for itself, a VML copy for older readers."""
    return parse_xml(
        f'<w:r {nsdecls("w")} xmlns:mc="{_MC}" xmlns:v="{_VML}" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx>'
        f"<w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent>"
        "</wps:txbx></wps:wsp></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>"
        f"<w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent>"
        "</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
    )


def _act(tmp_path: Path, *, control: str | None = None, box: object | None = None) -> Path:
    doc = Document()
    doc.add_paragraph("მიღება-ჩაბარების აქტი")
    if control is not None:
        doc.element.body.insert(1, _content_control(control))
    anchor = doc.add_paragraph("ხელმოწერა")
    if box is not None:
        anchor._p.append(box)
    doc.add_paragraph("ბოლო აბზაცი.")
    path = tmp_path / "act.docx"
    doc.save(str(path))
    return path


def _tracked(path: Path) -> tuple[list[str], list[str]]:
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    deleted = ["".join(t.text or "" for t in d.iter(f"{_W}delText")) for d in root.iter(f"{_W}del")]
    inserted = ["".join(t.text or "" for t in i.iter(f"{_W}t")) for i in root.iter(f"{_W}ins")]
    return deleted, inserted


def test_a_value_in_a_content_control_is_marked_where_it_stands(tmp_path: Path) -> None:
    src = _act(tmp_path, control="საიდენტიფიკაციო კოდი: 987654312")
    out, unanchored = annotate_docx(src, [_finding("987654312")], tmp_path / "o.docx")
    assert unanchored == []
    assert list(comment_anchors(out).values()) == ["987654312"]
    assert _tracked(out) == (["987654312"], ["987654321"])


def test_a_value_in_a_text_box_is_marked_in_the_box(tmp_path: Path) -> None:
    src = _act(tmp_path, box=_vml_box("კოდი: 987654312"))
    out, unanchored = annotate_docx(src, [_finding("987654312")], tmp_path / "o.docx")
    assert unanchored == []
    assert list(comment_anchors(out).values()) == ["987654312"]


def test_a_text_box_written_twice_is_marked_once_in_the_copy_word_shows(tmp_path: Path) -> None:
    src = _act(tmp_path, box=_both_boxes("კოდი: 987654312"))
    out, unanchored = annotate_docx(src, [_finding("987654312")], tmp_path / "o.docx")
    assert unanchored == []
    assert list(comment_anchors(out).values()) == ["987654312"]
    with zipfile.ZipFile(out) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    [start] = root.iter(f"{_W}commentRangeStart")
    assert any(a.tag == f"{{{_MC}}}Choice" for a in start.iterancestors())


def test_reading_order_takes_a_control_where_it_stands_and_a_box_after_its_anchor(
    tmp_path: Path,
) -> None:
    src = _act(tmp_path, control="კონტროლი", box=_both_boxes("ველი"))
    texts = [p.text for p in paragraphs_in_order(Document(str(src)))]
    assert texts == ["მიღება-ჩაბარების აქტი", "კონტროლი", "ხელმოწერა", "ველი", "ბოლო აბზაცი."]


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed")
def test_libreoffice_opens_a_form_marked_in_its_controls_and_boxes(tmp_path: Path) -> None:
    src = _act(tmp_path, control="კოდი: 987654312", box=_vml_box("კოდი: 987654312"))
    out, _ = annotate_docx(src, [_finding("987654312")], tmp_path / "o.docx")
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "odt", "--outdir", str(tmp_path), str(out)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    with zipfile.ZipFile(tmp_path / "o.odt") as z:
        content = z.read("content.xml").decode()
    assert content.count("<office:annotation ") >= 1

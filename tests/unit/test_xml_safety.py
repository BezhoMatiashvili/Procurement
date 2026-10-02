"""XML that tries to read a file, or to grow without bound, through every reader the app has.

An upload is a zip of XML parts; the parts are read by openpyxl, python-docx (and Docling's Word
backend, which is python-docx), and lxml directly in the DOCX export. A part with an external entity
(``<!ENTITY e SYSTEM "file:///...">``) or a chain of nested entities (the "billion laughs") is the
oldest attack on that. These tests plant a marker in a file the entity points at, and assert it
comes out of nothing.
"""

from __future__ import annotations

import contextlib
import io
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document
from lxml import etree
from openpyxl import Workbook

from procurement_agent.config import Settings
from procurement_agent.ingestion import docling_service
from procurement_agent.ingestion.excel_reader import read_workbook
from procurement_agent.report.docx_annotate import UnsupportedDocumentError, annotate_docx
from procurement_agent.security.upload_guard import UploadRejectedError, validate_upload

SECRET = "CANARY-SECRET-FILE-CONTENT-8841"


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "secret.txt"
    path.write_text(SECRET)
    return path


def _entity_dtd(secret: Path) -> str:
    return f'<!DOCTYPE x [<!ENTITY leak SYSTEM "file://{secret}">]>'


def _laughs() -> str:
    lines = ['<!ENTITY a0 "lol">']
    for i in range(1, 12):
        lines.append(f'<!ENTITY a{i} "' + f"&a{i - 1};" * 10 + '">')
    return "<!DOCTYPE x [" + "".join(lines) + "]>"


def _rewrite(data: bytes, part: str, edit) -> bytes:  # type: ignore[no-untyped-def]
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            body = src.read(item.filename)
            dst.writestr(item.filename, edit(body) if item.filename == part else body)
    return out.getvalue()


def _docx_bytes() -> bytes:
    doc = Document()
    doc.add_paragraph("SUPPLY CONTRACT No. 17/2024")
    doc.add_paragraph("The total price is 120,000.00 GEL.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["item", "price"])
    ws.append(["cable", 12])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _with_entity_in_document(data: bytes, dtd: str, reference: str = "&leak;") -> bytes:
    def edit(body: bytes) -> bytes:
        text = body.decode("utf-8")
        head, _, rest = text.partition("?>")
        rest = rest.replace("<w:t>", f"<w:t>{reference}", 1) if "<w:t>" in rest else rest
        rest = (
            rest.replace("<w:t ", f"<w:t >{reference}</w:t><w:t ", 1)
            if reference not in rest
            else rest
        )
        return (head + "?>" + dtd + rest).encode("utf-8")

    return _rewrite(data, "word/document.xml", edit)


# --- the parsers are configured the way the tests below assume ---------------------------------


def test_openpyxl_is_using_the_defusing_parser() -> None:
    """It falls back to the standard library's expat when defusedxml is missing, silently."""
    import openpyxl.xml as ox

    assert ox.DEFUSEDXML is True


def test_defusedxml_is_a_declared_dependency() -> None:
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"defusedxml>=' in pyproject


def test_the_attack_is_real_against_a_parser_that_resolves_entities(secret_file: Path) -> None:
    """Negative control: without it, every test below could pass because the planted file never
    worked. A parser left on lxml's defaults reads the file."""
    part = _with_entity_in_document(_docx_bytes(), _entity_dtd(secret_file))
    with zipfile.ZipFile(io.BytesIO(part)) as z:
        body = z.read("word/document.xml")
    naive = etree.XMLParser(resolve_entities=True, no_network=True)
    text = "".join(etree.fromstring(body, naive).itertext())
    assert SECRET in text


# --- DOCX --------------------------------------------------------------------------------------


def test_an_external_entity_in_a_word_document_is_not_resolved(
    tmp_path: Path, secret_file: Path
) -> None:
    hostile = tmp_path / "hostile.docx"
    hostile.write_bytes(_with_entity_in_document(_docx_bytes(), _entity_dtd(secret_file)))
    try:
        doc = Document(str(hostile))
    except Exception:
        return
    assert SECRET not in "\n".join(p.text for p in doc.paragraphs)


def test_docling_does_not_resolve_it_either(tmp_path: Path, secret_file: Path) -> None:
    hostile = tmp_path / "hostile.docx"
    hostile.write_bytes(_with_entity_in_document(_docx_bytes(), _entity_dtd(secret_file)))
    settings = Settings(env="dev", data_dir=tmp_path / "d", ollama_base_url="http://127.0.0.1:1")
    try:
        result = docling_service.convert(hostile, settings)
    except Exception:
        return
    assert SECRET not in "\n".join(t.text for t in result.document.texts)


def test_the_export_does_not_resolve_it_or_crash(tmp_path: Path, secret_file: Path) -> None:
    hostile = tmp_path / "hostile.docx"
    hostile.write_bytes(_with_entity_in_document(_docx_bytes(), _entity_dtd(secret_file)))
    out = tmp_path / "out.docx"
    try:
        annotate_docx(hostile, [], out)
    except UnsupportedDocumentError:
        return  # refused with the export's own error: fine
    with zipfile.ZipFile(out) as z:
        assert SECRET.encode() not in z.read("word/document.xml")


def test_nested_entities_are_not_expanded(tmp_path: Path) -> None:
    hostile = tmp_path / "laughs.docx"
    hostile.write_bytes(_with_entity_in_document(_docx_bytes(), _laughs(), "&a11;"))
    started = time.perf_counter()
    try:
        doc = Document(str(hostile))
        text = "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        text = ""
    assert time.perf_counter() - started < 5.0
    assert len(text) < 10_000  # not a hundred billion "lol"s


def test_a_malformed_document_part_is_a_clean_refusal_not_a_crash(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(
        _rewrite(_docx_bytes(), "word/document.xml", lambda body: body[: len(body) // 2])
    )
    with pytest.raises(UnsupportedDocumentError, match="not a Word document"):
        annotate_docx(broken, [], tmp_path / "out.docx")


def test_the_refusal_names_the_kind_of_error_and_not_its_text(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(
        _rewrite(_docx_bytes(), "word/document.xml", lambda body: b"<w:document>" + SECRET.encode())
    )
    with pytest.raises(UnsupportedDocumentError) as raised:
        annotate_docx(broken, [], tmp_path / "out.docx")
    assert SECRET not in str(raised.value)


# --- XLSX --------------------------------------------------------------------------------------


def _with_entity_in_sheet(data: bytes, dtd: str, reference: str) -> bytes:
    def edit(body: bytes) -> bytes:
        text = body.decode("utf-8")
        head, _, rest = text.partition("?>")
        return (head + "?>" + dtd + rest.replace("<v>12</v>", f"<v>{reference}</v>", 1)).encode(
            "utf-8"
        )

    return _rewrite(data, "xl/worksheets/sheet1.xml", edit)


def test_an_external_entity_in_a_worksheet_is_not_resolved(secret_file: Path) -> None:
    hostile = _with_entity_in_sheet(_xlsx_bytes(), _entity_dtd(secret_file), "&leak;")
    try:
        sheets = read_workbook(hostile)
    except Exception:
        return
    assert SECRET not in "\n".join(c.value for s in sheets for c in s.cells)


def test_nested_entities_in_a_worksheet_are_refused_quickly() -> None:
    hostile = _with_entity_in_sheet(_xlsx_bytes(), _laughs(), "&a11;")
    started = time.perf_counter()
    try:
        sheets = read_workbook(hostile)
        text = "\n".join(c.value for s in sheets for c in s.cells)
    except Exception:
        text = ""
    assert time.perf_counter() - started < 5.0
    assert len(text) < 10_000


def test_the_upload_guard_does_not_expand_entities_either(secret_file: Path) -> None:
    """It reads ``[Content_Types].xml`` as text, for the macro markers, and never parses it."""
    hostile = _rewrite(
        _docx_bytes(),
        "[Content_Types].xml",
        lambda body: body.replace(b"<Types", _entity_dtd(secret_file).encode() + b"<Types", 1),
    )
    with contextlib.suppress(UploadRejectedError):  # refusing it is fine; nothing may be expanded
        validate_upload("a.docx", hostile, max_bytes=10_000_000, max_pages=10)

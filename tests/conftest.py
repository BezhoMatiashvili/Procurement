"""Shared fixtures: synthetic in-memory documents. No real documents are ever used in tests."""

from __future__ import annotations

import io
import zipfile

import pytest

MIN_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n"
    b"0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n168\n%%EOF\n"
)

_CT_DOCX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/></Types>'
)
_CT_XLSX = _CT_DOCX.replace("/word/document.xml", "/xl/workbook.xml").replace(
    "wordprocessingml.document.main", "spreadsheetml.sheet.main"
)


def make_ooxml(
    kind: str, *, extra: dict[str, bytes] | None = None, content_types: str | None = None
) -> bytes:
    part = "word/document.xml" if kind == "docx" else "xl/workbook.xml"
    default_ct = _CT_DOCX if kind == "docx" else _CT_XLSX
    ct = content_types if content_types is not None else default_ct
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr(part, "<root/>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def min_pdf() -> bytes:
    return MIN_PDF


@pytest.fixture
def min_docx() -> bytes:
    return make_ooxml("docx")


@pytest.fixture
def min_xlsx() -> bytes:
    return make_ooxml("xlsx")

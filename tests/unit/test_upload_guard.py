from __future__ import annotations

import io
import random
import string
import warnings
import zipfile

import pytest

from procurement_agent.security import upload_guard
from procurement_agent.security.upload_guard import (
    UploadRejectedError,
    sanitize_filename,
    validate_upload,
)
from tests.conftest import make_ooxml

LIMITS = {"max_bytes": 1024 * 1024, "max_pages": 10}


# --- filename sanitisation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("offer.pdf", "offer.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\x\\ხელშეკრულება.docx", "ხელშეკრულება.docx"),
        ("bad\x00name\x1f.xlsx", "badname.xlsx"),
        ("...", "document"),
        ("", "document"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_extension() -> None:
    name = sanitize_filename("a" * 500 + ".pdf", max_len=50)
    assert len(name) <= 50
    assert name.endswith(".pdf")


# --- extension / kind ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["x.doc", "x.xls", "x.docm", "x.xlsm", "x.xlsb", "x.dotm"])
def test_blocked_extensions(name: str, min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError):
        validate_upload(name, min_pdf, **LIMITS)


@pytest.mark.parametrize("name", ["x.exe", "x.txt", "x", "x.pdf.sh", "x.PDF.exe"])
def test_unknown_extension(name: str, min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError, match="unsupported"):
        validate_upload(name, min_pdf, **LIMITS)


def test_allowed_kinds_is_respected(min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError, match="unsupported"):
        validate_upload("x.pdf", min_pdf, allowed_kinds=("docx",), **LIMITS)


def test_uppercase_extension_ok(min_pdf: bytes) -> None:
    assert validate_upload("OFFER.PDF", min_pdf, **LIMITS).kind == "pdf"


# --- size --------------------------------------------------------------------------------------


def test_empty_file(min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError, match="empty"):
        validate_upload("x.pdf", b"", **LIMITS)


def test_too_large(min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError, match="too large"):
        validate_upload("x.pdf", min_pdf + b"0" * (2 * 1024 * 1024), **LIMITS)


# --- PDF ---------------------------------------------------------------------------------------


def test_valid_pdf(min_pdf: bytes) -> None:
    v = validate_upload("offer.pdf", min_pdf, **LIMITS)
    assert v.kind == "pdf"
    assert v.page_count == 1
    assert v.display_name == "offer.pdf"


def test_pdf_with_wrong_magic(min_docx: bytes) -> None:
    with pytest.raises(UploadRejectedError, match="not a PDF"):
        validate_upload("x.pdf", min_docx, **LIMITS)


def test_pdf_garbage() -> None:
    with pytest.raises(UploadRejectedError, match="not a PDF"):
        validate_upload("x.pdf", b"hello world", **LIMITS)


def test_pdf_corrupt_after_header() -> None:
    with pytest.raises(UploadRejectedError, match="could not be opened"):
        validate_upload("x.pdf", b"%PDF-1.7\ngarbage", **LIMITS)


def test_pdf_too_many_pages(min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError, match="pages"):
        validate_upload("x.pdf", min_pdf, max_bytes=LIMITS["max_bytes"], max_pages=0)


# --- OOXML -------------------------------------------------------------------------------------


def test_valid_docx(min_docx: bytes) -> None:
    v = validate_upload("contract.docx", min_docx, **LIMITS)
    assert v.kind == "docx"
    assert v.page_count is None


def test_valid_xlsx(min_xlsx: bytes) -> None:
    assert validate_upload("boq.xlsx", min_xlsx, **LIMITS).kind == "xlsx"


def test_docx_uploaded_as_xlsx(min_docx: bytes) -> None:
    with pytest.raises(UploadRejectedError, match=r"is a \.docx"):
        validate_upload("x.xlsx", min_docx, **LIMITS)


def test_ooxml_not_a_zip() -> None:
    with pytest.raises(UploadRejectedError, match="not a valid"):
        validate_upload("x.docx", b"not a zip at all", **LIMITS)


def test_ooxml_ole_disguised() -> None:
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    with pytest.raises(UploadRejectedError, match="legacy binary"):
        validate_upload("x.docx", ole, **LIMITS)


def test_ooxml_missing_content_types() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<r/>")
    with pytest.raises(UploadRejectedError, match="not a valid"):
        validate_upload("x.docx", buf.getvalue(), **LIMITS)


def test_ooxml_macro_part_rejected() -> None:
    data = make_ooxml("docx", extra={"word/vbaProject.bin": b"\x00" * 10})
    with pytest.raises(UploadRejectedError, match="macros"):
        validate_upload("x.docx", data, **LIMITS)


def test_ooxml_macro_content_type_rejected() -> None:
    ct = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'
        "</Types>"
    )
    data = make_ooxml("docx", content_types=ct)
    with pytest.raises(UploadRejectedError, match="macros"):
        validate_upload("x.docx", data, **LIMITS)


def test_ooxml_path_traversal() -> None:
    data = make_ooxml("docx", extra={"../../evil.txt": b"x"})
    with pytest.raises(UploadRejectedError, match="unsafe paths"):
        validate_upload("x.docx", data, **LIMITS)


def test_ooxml_zip_bomb_ratio() -> None:
    data = make_ooxml("docx", extra={"word/media/blob.bin": b"\x00" * (5 * 1024 * 1024)})
    with pytest.raises(UploadRejectedError, match="compression ratio"):
        validate_upload("x.docx", data, **LIMITS)


def test_ooxml_encrypted_entry_rejected(min_docx: bytes) -> None:
    # zipfile resets flag_bits on write, so flip the "encrypted" bit in the central directory
    data = bytearray(min_docx)
    sig = b"PK\x01\x02"
    idx = data.find(sig)
    assert idx != -1
    data[idx + 8] |= 0x1
    with pytest.raises(UploadRejectedError, match="encrypted"):
        validate_upload("x.docx", bytes(data), **LIMITS)


# --- invisible characters, echoed extensions, part sizes, duplicate parts ---------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inv‮oice‬.pdf", "invoice.pdf"),  # bidi overrides: "fdp.exe" reading as "exe.pdf"
        ("a​b‌.pdf", "ab.pdf"),  # zero-width space and non-joiner
        ("﻿offer.pdf", "offer.pdf"),  # byte-order mark
        ("x\x85y z .pdf", "xyz.pdf"),  # a C1 control, line and paragraph separators
        ("ხელშეკრულება.docx", "ხელშეკრულება.docx"),  # real Georgian text is untouched
    ],
)
def test_sanitize_filename_drops_invisible_characters(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_an_extension_that_is_not_one_is_not_echoed(min_pdf: bytes) -> None:
    """The message reaches the audit trail and the log; the caller's own text must not."""
    with pytest.raises(UploadRejectedError) as raised:
        validate_upload("project-alpha-draft.confidential-v2", min_pdf, **LIMITS)
    assert "confidential" not in str(raised.value)
    assert "unsupported file type (unrecognised)" in str(raised.value)


def test_a_plausible_extension_is_still_shown(min_pdf: bytes) -> None:
    with pytest.raises(UploadRejectedError, match=r"unsupported file type \.exe"):
        validate_upload("x.exe", min_pdf, **LIMITS)


def test_ooxml_duplicate_entries_rejected(min_docx: bytes) -> None:
    buf = io.BytesIO(min_docx)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile warns about the very thing tested
        with zipfile.ZipFile(buf, "a") as zf:
            zf.writestr("word/document.xml", "<other/>")
    with pytest.raises(UploadRejectedError, match="duplicate entries"):
        validate_upload("x.docx", buf.getvalue(), **LIMITS)


def test_ooxml_content_types_part_over_a_megabyte_rejected() -> None:
    filler = "".join(random.Random(1).choices(string.ascii_letters, k=1024 * 1024 + 10))
    data = make_ooxml("docx", content_types=f"<Types>{filler}</Types>")
    with pytest.raises(UploadRejectedError, match="content-type list is too large"):
        validate_upload("x.docx", data, max_bytes=8 * 1024 * 1024, max_pages=10)


def test_ooxml_single_part_over_the_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_guard, "_MAX_MEMBER_BYTES", 1000)
    blob = random.Random(2).randbytes(2000)  # incompressible, so the ratio check is not what fires
    data = make_ooxml("docx", extra={"word/media/big.bin": blob})
    with pytest.raises(UploadRejectedError, match="part is too large"):
        validate_upload("x.docx", data, **LIMITS)

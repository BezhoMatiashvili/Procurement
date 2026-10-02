"""Validation of uploaded files before anything touches them.

Every uploaded document is treated as hostile. We check, in order:
  1. sanitized display name / extension allowlist
  2. size limit
  3. magic bytes matching the claimed type
  4. container-level checks (ZIP bomb, path traversal, embedded macros for OOXML;
     encryption and page count for PDF)

Only ``pdf``, ``docx`` and ``xlsx`` are accepted in phase 1. Legacy binary Office formats and
macro-enabled variants are rejected outright.
"""

from __future__ import annotations

import io
import itertools
import re
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from procurement_agent.config import DocumentKind
from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK
from procurement_agent.security.safe_errors import ContentFree

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_EXT_TO_KIND: dict[str, DocumentKind] = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}
_BLOCKED_EXT_HINT = {
    ".doc": "legacy .doc is not accepted; save as .docx",
    ".xls": "legacy .xls is not accepted; save as .xlsx",
    ".docm": "macro-enabled documents are not accepted",
    ".xlsm": "macro-enabled workbooks are not accepted",
    ".xlsb": "binary workbooks are not accepted; save as .xlsx",
    ".dotm": "macro-enabled templates are not accepted",
}

# ZIP-bomb guard: OOXML compresses well but a 50 MB docx should never inflate past this.
_MAX_ZIP_ENTRIES = 10_000
_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
#: No single part - a sheet, the document body - may inflate past this; the total above alone
#: let one part take it all, and a parser builds a tree several times its size.
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
#: ``[Content_Types].xml`` is a list of a few dozen entries; the guard reads it whole.
_MAX_CONTENT_TYPES_BYTES = 1024 * 1024

#: Unicode categories no file name needs and some only use to lie about it: controls (C0, DEL,
#: C1), format characters (the bidi overrides that make "fdp.exe" read as "exe.pdf", zero-width
#: spaces, the byte-order mark) and the line and paragraph separators.
_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})
#: What a real extension looks like. Anything else is the caller's text, and is not echoed.
_PLAUSIBLE_EXT = re.compile(r"\.[a-z0-9]{1,5}")


class UploadRejectedError(ContentFree, ValueError):
    """Raised with a user-safe message explaining why the upload was refused."""


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    display_name: str
    kind: DocumentKind
    size_bytes: int
    page_count: int | None


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Return a display-safe filename (never used as a filesystem path)."""
    base = PurePosixPath(name.replace("\\", "/")).name
    base = unicodedata.normalize("NFC", base)
    base = "".join(ch for ch in base if unicodedata.category(ch) not in _INVISIBLE)
    base = base.strip().strip(".")
    if not base:
        base = "document"
    if len(base) > max_len:
        stem, dot, ext = base.rpartition(".")
        keep = max_len - len(ext) - 1 if dot else max_len
        base = (stem[:keep] + "." + ext) if dot else base[:max_len]
    return base


def _shown_extension(ext: str) -> str:
    """The extension as a message may say it. The message reaches the audit trail and the log,
    which never carry text the caller chose; an extension that does not look like one is that
    text."""
    if not ext:
        return "(none)"
    return ext if _PLAUSIBLE_EXT.fullmatch(ext) else "(unrecognised)"


def validate_upload(
    filename: str,
    data: bytes,
    *,
    max_bytes: int,
    max_pages: int,
    allowed_kinds: tuple[DocumentKind, ...] = ("pdf", "docx", "xlsx"),
) -> ValidatedUpload:
    display = sanitize_filename(filename)
    ext = ("." + display.rsplit(".", 1)[1].lower()) if "." in display else ""

    if ext in _BLOCKED_EXT_HINT:
        raise UploadRejectedError(_BLOCKED_EXT_HINT[ext])
    kind = _EXT_TO_KIND.get(ext)
    if kind is None or kind not in allowed_kinds:
        allowed = ", ".join(f".{k}" for k in allowed_kinds)
        msg = f"unsupported file type {_shown_extension(ext)}; allowed: {allowed}"
        raise UploadRejectedError(msg)

    if not data:
        raise UploadRejectedError("empty file")
    if len(data) > max_bytes:
        mb = 1024 * 1024
        msg = f"file too large ({len(data) // mb} MB); limit is {max_bytes // mb} MB"
        raise UploadRejectedError(msg)

    page_count: int | None = None
    if kind == "pdf":
        page_count = _check_pdf(data, max_pages)
    else:
        _check_ooxml(data, kind)

    return ValidatedUpload(display, kind, len(data), page_count)


# --- PDF -------------------------------------------------------------------------------------


def _check_pdf(data: bytes, max_pages: int) -> int:
    # Some generators prepend junk (e.g. a UTF-8 BOM); the spec allows the header within 1024 bytes.
    if _PDF_MAGIC not in data[:1024]:
        raise UploadRejectedError("file content is not a PDF")
    if data[:8] == _OLE_MAGIC or data[:4] == _ZIP_MAGIC:
        raise UploadRejectedError("file content is not a PDF")

    import pypdfium2 as pdfium

    with PDFIUM_LOCK:  # pdfium is not thread-safe, not even across documents
        try:
            pdf = pdfium.PdfDocument(io.BytesIO(data))
        except pdfium.PdfiumError as e:  # includes password-protected files
            msg = "PDF could not be opened (corrupt or password protected)"
            raise UploadRejectedError(msg) from e
        try:
            n = len(pdf)
        finally:
            pdf.close()
    if n == 0:
        raise UploadRejectedError("PDF has no pages")
    if n > max_pages:
        msg = f"PDF has {n} pages; limit is {max_pages}"
        raise UploadRejectedError(msg)
    return n


# --- OOXML (docx / xlsx) ---------------------------------------------------------------------

_REQUIRED_PART: dict[str, str] = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}
#: What ``zipfile`` raises for a member that cannot be read: bad CRC or magic (``BadZipFile``), an
#: offset before the start of the file (``ValueError``), a truncated stream (``EOFError``,
#: ``zlib.error``), a compression method it lacks (``NotImplementedError``), a flag it will not
#: handle (``RuntimeError``), the file itself (``OSError``).
_CORRUPT_ARCHIVE = (
    zipfile.BadZipFile,
    ValueError,
    EOFError,
    zlib.error,
    NotImplementedError,
    RuntimeError,
    OSError,
)
_MACRO_MARKERS = ("vbaproject.bin", "vbadata.xml")


def _check_ooxml(data: bytes, kind: DocumentKind) -> None:
    if data[:4] != _ZIP_MAGIC:
        if data[:8] == _OLE_MAGIC:
            msg = f"file is a legacy binary Office document, not .{kind}"
            raise UploadRejectedError(msg)
        msg = f"file content is not a valid .{kind}"
        raise UploadRejectedError(msg)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except _CORRUPT_ARCHIVE as e:  # BadZipFile, and "zip file version 25.5" (NotImplementedError)
        msg = f"file content is not a valid .{kind} (corrupt archive)"
        raise UploadRejectedError(msg) from e

    with zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise UploadRejectedError("archive has too many entries")

        total_uncompressed = 0
        names: set[str] = set()
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in PurePosixPath(name).parts or ":" in name[:3]:
                raise UploadRejectedError("archive contains unsafe paths")
            if info.flag_bits & 0x1:
                raise UploadRejectedError("encrypted archives are not accepted")
            if info.file_size > _MAX_MEMBER_BYTES:
                raise UploadRejectedError("an archive part is too large")
            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
                raise UploadRejectedError("archive inflates beyond the allowed size")
            if (
                info.compress_size
                and info.file_size / max(info.compress_size, 1) > _MAX_COMPRESSION_RATIO
            ):
                raise UploadRejectedError("archive has a suspicious compression ratio")
            # Two parts of one name: parsers disagree about which they read, so a check on one
            # says nothing about the other.
            if name.lower() in names:
                raise UploadRejectedError("archive has duplicate entries")
            names.add(name.lower())
            if any(m in name.lower() for m in _MACRO_MARKERS):
                raise UploadRejectedError("documents containing macros are not accepted")

        # Entries whose data overlaps (several directory entries naming the same bytes) are how a
        # small archive claims to hold gigabytes - the "zip quine" - and no office program writes
        # them. The local header is at least 30 bytes plus the name, so an honest archive never
        # trips this: its next entry starts after the previous one's data.
        spans = sorted(
            (
                i.header_offset,
                i.header_offset + 30 + len(i.filename.encode("utf-8")) + i.compress_size,
            )
            for i in infos
        )
        if any(end > next_start for (_, end), (next_start, _) in itertools.pairwise(spans)):
            raise UploadRejectedError("archive entries overlap")

        if "[content_types].xml" not in names:
            msg = f"file content is not a valid .{kind}"
            raise UploadRejectedError(msg)
        if _REQUIRED_PART[kind].lower() not in names:
            other = "docx" if kind == "xlsx" else "xlsx"
            if _REQUIRED_PART[other].lower() in names:
                msg = f"file is a .{other} but was uploaded as .{kind}"
                raise UploadRejectedError(msg)
            msg = f"file content is not a valid .{kind}"
            raise UploadRejectedError(msg)

        # Content-Types is authoritative for the macro-enabled variants even if the part is renamed.
        try:
            content_types = zf.getinfo("[Content_Types].xml")
        except KeyError:
            ctypes = ""
        else:
            if content_types.file_size > _MAX_CONTENT_TYPES_BYTES:
                raise UploadRejectedError("the archive's content-type list is too large")
            try:
                ctypes = zf.read(content_types).decode("utf-8", "replace").lower()
            except _CORRUPT_ARCHIVE as e:
                # A bad CRC, a header offset that points nowhere, an unsupported compression method:
                # the central directory read fine and the member did not. A refusal, not a 500.
                msg = f"file content is not a valid .{kind} (corrupt archive)"
                raise UploadRejectedError(msg) from e
        if "macroenabled" in ctypes or "vnd.ms-office.vbaproject" in ctypes:
            raise UploadRejectedError("documents containing macros are not accepted")

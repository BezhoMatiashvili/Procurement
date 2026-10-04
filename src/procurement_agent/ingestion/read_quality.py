"""How much of a document was read, kept as numbers beside its parse.

A parse that comes back is not a parse that read the document: a scanned page with no OCR yields
no text, a conversion that ran out of time yields the first few pages, and a Word file can have a
part the reader could not process. None of that stops Docling from returning a document, and a
check run on what came back looks exactly like a check run on the whole - "no findings" about
text that was never read. So every parse is assessed once, when it is made, and the answer is
stored in ``results/<id>/read_quality.json`` next to ``docling.json``:

* a document with **no readable text** at all is refused (``NoTextError``; the parse job fails
  and the document is ``failed`` with the code ``NO_TEXT``), because everything built on it would
  be an empty result that reads as a clean one;
* a document with **some of its pages unread** stays ``ready`` but is ``partial``, and every
  report built from it says how many pages had no readable text (``DOC.PARTIAL``).

The sidecar holds numbers, booleans and the file kind - never a word of the document - so it
may be logged and returned by the API as it is. A document stored before this module existed has
no sidecar; ``read_quality`` then works the same answer out of the stored parse.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import pypdfium2
from docling.datamodel.base_models import ConversionStatus
from docling.datamodel.document import ConversionResult
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument

from procurement_agent.checks.findings import SkipNote
from procurement_agent.config import Settings
from procurement_agent.ingestion import docling_service
from procurement_agent.ingestion.chunker import Chunk
from procurement_agent.ingestion.docling_service import load_document
from procurement_agent.ingestion.excel_reader import read_workbook, sheet_chunks
from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK
from procurement_agent.security.safe_errors import ContentFree, safe_exc

__all__ = [
    "DOCUMENT_UNREADABLE",
    "IMAGE_ONLY",
    "NO_TEXT",
    "OCR_UNAVAILABLE",
    "PARTIAL_RULE",
    "NoTextError",
    "ReadQuality",
    "StoredParseError",
    "assess_chunks",
    "assess_docling",
    "compute_quality",
    "load_stored",
    "parse_error",
    "partial_note",
    "pdf_page_count",
    "read_quality",
    "read_sidecar",
    "settle_conversion",
    "settle_parse",
    "settle_workbook",
    "sidecar_path",
    "split_error",
    "write_quality",
]

#: The parse produced no text anywhere: the document is ``failed`` with this code.
NO_TEXT = "NO_TEXT"
#: Hints that name the cause when it is known: a PDF read without OCR, a PDF read with OCR that
#: still found nothing (pictures, or a page too poor to recognise).
OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
IMAGE_ONLY = "IMAGE_ONLY"
#: The stored parse is missing or damaged: the document cannot be read again from what is kept.
DOCUMENT_UNREADABLE = "DOCUMENT_UNREADABLE"
#: The id of the note every report from a partly read document carries.
PARTIAL_RULE = "DOC.PARTIAL"

_SIDECAR = "read_quality.json"
_BODY = ContentLayer.BODY


class NoTextError(ContentFree, RuntimeError):
    """The parse holds no text. The message is the code (and the hint), nothing else."""

    code = NO_TEXT

    def __init__(self, hint: str | None = None) -> None:
        self.hint = hint
        super().__init__(f"{NO_TEXT}:{hint}" if hint else NO_TEXT)


class StoredParseError(ContentFree, RuntimeError):
    """The parse stored for a document is missing or cannot be loaded. ``document_id`` is ours."""

    def __init__(self, message: str, document_id: str | None = None) -> None:
        super().__init__(message)
        self.document_id = document_id


@dataclass(frozen=True, slots=True)
class ReadQuality:
    """What was read of one document. Numbers and flags only."""

    kind: str
    #: Pages in the file, or None where the format has none (Word, Excel).
    pages_total: int | None
    #: Pages that carry at least one letter or digit of body text, or None with ``pages_total``.
    pages_with_text: int | None
    #: Letters and digits of body text read. Zero means nothing was read.
    text_chars: int
    #: Part of the document was not read: the conversion said so, or pages have no text.
    partial: bool
    #: Errors the conversion reported.
    errors: int = 0
    #: Whether OCR was available when the document was parsed; None where it does not matter
    #: or is not known (a document stored before this was recorded).
    ocr: bool | None = None

    @property
    def has_text(self) -> bool:
        return self.text_chars > 0

    @property
    def pages_without_text(self) -> int:
        if self.pages_total is None or self.pages_with_text is None:
            return 0
        return max(self.pages_total - self.pages_with_text, 0)


class _Identified(Protocol):
    """A stored document: the ORM row or ``StoredDocument`` - all this module needs."""

    @property
    def id(self) -> str: ...
    @property
    def kind(self) -> str: ...


def _readable(text: str | None) -> int:
    """Letters and digits in ``text``: punctuation and spaces are not something to read."""
    return sum(1 for ch in text or "" if ch.isalnum())


def pdf_page_count(path: Path) -> int | None:
    """Pages in the PDF file itself, or None when pdfium cannot open it. The conversion's own
    page table can be cut short by a timeout; the file cannot."""
    with PDFIUM_LOCK:
        try:
            pdf = pypdfium2.PdfDocument(str(path))
        except (pypdfium2.PdfiumError, OSError):
            return None
        try:
            return len(pdf)
        finally:
            pdf.close()


def assess_docling(
    doc: DoclingDocument,
    kind: str,
    *,
    pdf_pages: int | None = None,
    conversion_partial: bool = False,
    errors: int = 0,
    ocr: bool | None = None,
) -> ReadQuality:
    """The quality of a parse: body text per page, against the pages the file has.

    Furniture (running headers and footers) is not text read: a scanned page with a printed
    footer is still a scan. A table's cells count for the page the table is on.
    """
    page_table = len(doc.pages)
    known = [n for n in (page_table, pdf_pages) if n]
    pages_total = max(known) if known else None
    pages_seen: set[int] = set()
    chars = 0

    def seen(chars_here: int, prov: Sequence[object]) -> None:
        if chars_here:
            for p in prov:
                page_no = getattr(p, "page_no", None)
                if isinstance(page_no, int):
                    pages_seen.add(page_no)

    for item in doc.texts:
        if item.content_layer != _BODY:
            continue
        n = _readable(item.text)
        chars += n
        seen(n, item.prov)
    for table in doc.tables:
        if table.content_layer != _BODY:
            continue
        n = sum(_readable(getattr(cell, "text", "")) for cell in table.data.table_cells)
        chars += n
        seen(n, table.prov)

    with_text = (
        sum(1 for n in pages_seen if 1 <= n <= pages_total) if pages_total is not None else None
    )
    partial = (
        conversion_partial
        or errors > 0
        or (pages_total is not None and with_text is not None and with_text < pages_total)
    )
    return ReadQuality(
        kind=kind,
        pages_total=pages_total,
        pages_with_text=with_text,
        text_chars=chars,
        partial=partial,
        errors=errors,
        ocr=ocr,
    )


def assess_chunks(chunks: Sequence[Chunk], kind: str = "xlsx") -> ReadQuality:
    """The quality of a workbook: it has no pages, so it is read or it is empty."""
    chars = sum(_readable(c.body or c.text) for c in chunks)
    return ReadQuality(
        kind=kind, pages_total=None, pages_with_text=None, text_chars=chars, partial=False
    )


# --- the sidecar -------------------------------------------------------------------------------


def sidecar_path(settings: Settings, doc_id: str) -> Path:
    return settings.results_dir / doc_id / _SIDECAR


def write_quality(settings: Settings, doc_id: str, quality: ReadQuality) -> Path:
    """Write the sidecar atomically (a temporary file in the same directory, then a rename), so
    a reader never meets half of it. Numbers only, so no overwrite-before-delete is needed."""
    target = sidecar_path(settings, doc_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".read_quality.", suffix=".json.tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(quality), fh)
        tmp.chmod(0o600)
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def _int_or_none(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def read_sidecar(settings: Settings, doc_id: str) -> ReadQuality | None:
    """The stored quality, or None when there is none or it is not one (damaged, hand-edited):
    the caller then works it out again from the parse."""
    try:
        data = json.loads(sidecar_path(settings, doc_id).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        kind, total, with_text = data["kind"], data["pages_total"], data["pages_with_text"]
        chars, partial = data["text_chars"], data["partial"]
        errors, ocr = data.get("errors", 0), data.get("ocr")
    except KeyError:
        return None
    if not (
        isinstance(kind, str)
        and _int_or_none(total)
        and _int_or_none(with_text)
        and isinstance(chars, int)
        and not isinstance(chars, bool)
        and isinstance(partial, bool)
        and isinstance(errors, int)
        and not isinstance(errors, bool)
        and (ocr is None or isinstance(ocr, bool))
    ):
        return None
    return ReadQuality(
        kind=kind,
        pages_total=total,
        pages_with_text=with_text,
        text_chars=chars,
        partial=partial,
        errors=errors,
        ocr=ocr,
    )


def load_stored(settings: Settings, doc_id: str) -> DoclingDocument:
    """The parse stored for a document. A file that is missing, truncated or not a parse at all
    is one thing to the caller - ``StoredParseError``, naming the document and the kind of
    failure, never what the file held - so every reader of a stored parse refuses the same way."""
    try:
        return load_document(settings.results_dir / doc_id / "docling.json")
    except Exception as e:
        msg = f"the stored reading of the document cannot be loaded ({type(e).__name__})"
        raise StoredParseError(msg, doc_id) from e


def compute_quality(settings: Settings, doc: _Identified) -> ReadQuality:
    """The quality of a stored document worked out from its files: for one parsed before the
    sidecar existed. Raises ``StoredParseError`` when the parse cannot be loaded."""
    upload = settings.upload_dir / f"{doc.id}.{doc.kind}"
    if doc.kind == "xlsx":
        try:
            chunks = sheet_chunks(read_workbook(upload.read_bytes()), doc_id=doc.id)
        except Exception as e:
            msg = f"the stored workbook cannot be read ({type(e).__name__})"
            raise StoredParseError(msg, doc.id) from e
        return assess_chunks(chunks)
    return assess_docling(
        load_stored(settings, doc.id),
        doc.kind,
        pdf_pages=pdf_page_count(upload) if doc.kind == "pdf" else None,
    )


def read_quality(settings: Settings, doc: _Identified) -> ReadQuality:
    """What was read of ``doc``: its sidecar, else worked out from the stored parse."""
    return read_sidecar(settings, doc.id) or compute_quality(settings, doc)


# --- settling a parse ---------------------------------------------------------------------------


def settle_parse(
    settings: Settings,
    doc_id: str,
    kind: str,
    doc: DoclingDocument,
    *,
    upload: Path | None,
    ocr: bool | None,
    conversion_partial: bool = False,
    errors: int = 0,
) -> ReadQuality:
    """Assess a fresh parse and store the answer; refuse it when it holds no text.

    The sidecar is written first and kept even for a refused parse: it is what says why.
    """
    quality = assess_docling(
        doc,
        kind,
        pdf_pages=pdf_page_count(upload) if upload is not None and kind == "pdf" else None,
        conversion_partial=conversion_partial,
        errors=errors,
        ocr=ocr,
    )
    write_quality(settings, doc_id, quality)
    if not quality.has_text:
        hint = None
        if kind == "pdf" and ocr is not None:
            hint = IMAGE_ONLY if ocr else OCR_UNAVAILABLE
        raise NoTextError(hint)
    return quality


def settle_conversion(
    settings: Settings, doc_id: str, kind: str, result: ConversionResult, *, upload: Path | None
) -> ReadQuality:
    """``settle_parse`` for what the converter returned: its status and error count say whether
    it finished, and whether OCR could run is read from the host, as the converter did."""
    return settle_parse(
        settings,
        doc_id,
        kind,
        result.document,
        upload=upload,
        ocr=docling_service.ocr_available(settings) if kind == "pdf" else None,
        conversion_partial=result.status == ConversionStatus.PARTIAL_SUCCESS,
        errors=len(result.errors),
    )


def settle_workbook(settings: Settings, doc_id: str, chunks: Sequence[Chunk]) -> ReadQuality:
    """The same for a workbook, read as chunks: one with no cell text is refused."""
    quality = assess_chunks(chunks)
    write_quality(settings, doc_id, quality)
    if not quality.has_text:
        raise NoTextError
    return quality


def parse_error(e: BaseException) -> str:
    """What a failed parse leaves in the document's ``error``: the code for one that read nothing
    (``NO_TEXT`` and its hint), and for any other failure the kind of it - never its message,
    which may quote the document (``security.safe_errors``)."""
    if isinstance(e, NoTextError):
        return str(e)
    return f"could not be read: {safe_exc(e)}"


def split_error(error: str | None) -> tuple[str | None, str | None]:
    """``(code, hint)`` from a document's ``error`` when it is one of ours (``NO_TEXT`` or
    ``NO_TEXT:OCR_UNAVAILABLE``, ``DOCUMENT_UNREADABLE``); ``(None, None)`` for anything else -
    the free text of an older failure."""
    if not error:
        return None, None
    code, _, hint = error.partition(":")
    if code in (NO_TEXT, DOCUMENT_UNREADABLE) and (not hint or hint.isupper()):
        return code, hint or None
    return None, None


# --- the note every report carries ----------------------------------------------------------------

#: Who the note is about: (English, Georgian). The side of a comparison, the act, the contract.
_WHO: dict[str, tuple[str, str]] = {
    "document": ("The document", "დოკუმენტი"),
    "act": ("The act", "აქტი"),
    "contract": ("The contract", "ხელშეკრულება"),
    "left": ("The first document", "პირველი დოკუმენტი"),
    "right": ("The second document", "მეორე დოკუმენტი"),
}


def partial_note(quality: ReadQuality, who: str = "document") -> SkipNote | None:
    """The bilingual note a report carries when ``quality`` is partial; None for a document read
    in full. It says how much was not read and that the findings may be incomplete."""
    if not quality.partial:
        return None
    en_who, ka_who = _WHO.get(who, _WHO["document"])
    missing, total = quality.pages_without_text, quality.pages_total
    if total and missing:
        en = (
            f"{en_who}: {missing} of {total} pages had no readable text (a scan without OCR, or "
            "a page the reader could not process); findings may be incomplete"
        )
        ka = (
            f"{ka_who}: {total} გვერდიდან {missing} გვერდზე წასაკითხი ტექსტი არ იყო (სკანი OCR-ის "
            "გარეშე ან გვერდი, რომელიც პროგრამამ ვერ დაამუშავა); დასკვნები შეიძლება არასრული იყოს"
        )
    else:
        en = (
            f"{en_who}: the reader reported problems with part of it and that part was not "
            "read; findings may be incomplete"
        )
        ka = (
            f"{ka_who}: პროგრამამ მისი ნაწილის წაკითხვისას შეცდომა დააფიქსირა და ეს ნაწილი არ "
            "წაიკითხა; დასკვნები შეიძლება არასრული იყოს"
        )
    return SkipNote(rule_id=PARTIAL_RULE, reason_ka=ka, reason_en=en)

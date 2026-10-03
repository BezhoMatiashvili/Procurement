"""Write findings into a PDF act as highlights with a note - the client's "mark it in the file
itself" for an act that arrives as a PDF.

A PDF with a text layer is searched in its own characters, as ``docx_annotate`` searches a Word
file (the finding's quote first, then the value it read, the correction last), with quotes,
spaces and look-alike letters set aside: a PDF's text often lacks the quote marks its font draws,
and breaks a phrase across lines. The words found are highlighted, one quad a line, and the
highlight's note is the finding's comment (``comment_text``): what is wrong and what it should
say.

A scanned page has no characters. What Docling's OCR read from it is known only as the boxes of
its lines and table cells (``docling_items``), so the line or cell holding the words is
highlighted, and the note says the line is marked, not the word.

Findings no words of the act stand for, and the summary of what could not be checked, go in a
note on the first page. Nothing but annotations is added: no action, no link, no script.

The annotated copy is a new document of new pages, each showing what the act's page shows - its
content, its own annotations drawn into it - and the notes: never the act's file with notes
added. What a PDF runs or carries when it opens stays behind, since a reviewer opens the copy as
the application's own output (red team R6, R6-08): the document's actions, scripts, attached
files and forms, and every entry of the page itself - its annotations and their actions, its
associated files, its metadata - for each page is drawn anew from its content alone (security
review). A page that runs an action itself when opened or closed is refused.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from procurement_agent.checks.findings import Finding
from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK
from procurement_agent.report.docx_annotate import (
    AUTHOR,
    MAX_PLACES,
    MAX_PLACES_PER_FINDING,
    MAX_SEARCHED,
    UnsupportedDocumentError,
    _candidates,
    _cut_short_note,
    _normalised_index,
    _offending,
    comment_text,
)
from procurement_agent.security.audit import log

#: A box on a page in PDF points, its origin at the bottom left: (left, bottom, right, top).
Box = tuple[float, float, float, float]

#: The highlight's colour by severity, as the report shows it.
_COLOURS = {"error": (230, 70, 60), "warning": (245, 160, 30), "info": (250, 215, 0)}
_OPACITY = 120
_SCANNED_NOTE = (
    "(სკანირებული გვერდი: მონიშნულია სტრიქონი, სადაც ეს სიტყვები წერია / "
    "a scanned page: the line holding these words is marked, not the words alone)"
)
#: Characters a PDF string should not carry: a note quotes the document, which may hold any. A
#: lone surrogate has no UTF-16 form at all; U+FFFE and U+FFFF are no characters.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


@dataclass(frozen=True, slots=True)
class PdfItem:
    """A line, a paragraph or a table cell of a scanned page as Docling read it: the page
    (from 1), its box in PDF points, and its text."""

    page: int
    box: Box
    text: str


@dataclass(frozen=True, slots=True)
class _Place:
    page: int  # from 0
    boxes: tuple[Box, ...]
    exact: bool  # the words themselves, or (a scan) the line that holds them


def docling_items(doc: Any) -> list[PdfItem]:
    """Every text item and table cell of a Docling document with its box on its page, for a
    scanned PDF. Boxes are turned to the PDF's own origin, the page's bottom left."""
    out: list[PdfItem] = []
    for item, _level in doc.iterate_items():
        cells = getattr(getattr(item, "data", None), "table_cells", None)
        for prov in getattr(item, "prov", None) or []:
            page = doc.pages.get(prov.page_no)
            if page is None:
                continue
            height = page.size.height
            if cells:
                for cell in cells:
                    if cell.bbox is not None and cell.text:
                        out.append(
                            PdfItem(prov.page_no, _bottom_left(cell.bbox, height), cell.text)
                        )
            elif getattr(item, "text", ""):
                out.append(PdfItem(prov.page_no, _bottom_left(prov.bbox, height), item.text))
    return out


def _bottom_left(bbox: Any, height: float) -> Box:
    b = bbox.to_bottom_left_origin(height)
    return (float(b.l), float(b.b), float(b.r), float(b.t))


def _whole(flat: str, start: int, end: int) -> bool:
    """Whether ``flat[start:end]`` stands as whole words (``docx_annotate._bounded``)."""
    if start > 0 and flat[start].isalnum() and flat[start - 1].isalnum():
        return False
    return not (end < len(flat) and flat[end - 1].isalnum() and flat[end].isalnum())


def _found(flat: str, needle: str, *, whole: bool) -> Iterator[tuple[int, int]]:
    at = flat.find(needle)
    while at >= 0:
        if not whole or _whole(flat, at, at + len(needle)):
            yield at, at + len(needle)
        at = flat.find(needle, at + 1)


def _lines(boxes: Sequence[Box]) -> list[Box]:
    """Character boxes merged into one box a line: a new line where a box starts left of the
    last one or lies wholly below it."""
    lines: list[list[Box]] = []
    for box in boxes:
        if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
            continue  # a space or a line break PDFium added: no box
        if lines and box[0] >= lines[-1][-1][0] - 1 and box[3] > lines[-1][-1][1]:
            lines[-1].append(box)
        else:
            lines.append([box])
    return [
        (min(b[0] for b in ln), min(b[1] for b in ln), max(b[2] for b in ln), max(b[3] for b in ln))
        for ln in lines
    ]


class _TextPages:
    """Each page's text as PDFium reads it, folded for search, with each folded character's
    index among the page's characters."""

    def __init__(self, pdf: pdfium.PdfDocument) -> None:
        self.pages: list[tuple[Any, str, list[int]]] = []
        for i in range(len(pdf)):
            textpage = pdf[i].get_textpage()
            text = textpage.get_text_range()
            flat, index = _normalised_index(text)
            self.pages.append((textpage, flat, index))

    @property
    def characters(self) -> int:
        return sum(len(flat) for _, flat, _ in self.pages)

    def places(self, needle: str, *, whole: bool, every: bool) -> list[_Place]:
        flat_needle, _ = _normalised_index(needle)
        if not flat_needle:
            return []
        out: list[_Place] = []
        for page, (textpage, flat, index) in enumerate(self.pages):
            for start, end in _found(flat, flat_needle, whole=whole):
                first, last = index[start], index[end - 1]
                boxes = [textpage.get_charbox(c, loose=False) for c in range(first, last + 1)]
                lines = _lines(boxes)
                if lines:
                    out.append(_Place(page, tuple(lines), exact=True))
                    if not every or len(out) > MAX_SEARCHED:
                        return out
        return out

    def close(self) -> None:
        for textpage, _, _ in self.pages:
            textpage.close()


class _ScannedPages:
    """A scanned PDF's lines and cells (``PdfItem``), folded for search."""

    def __init__(self, items: Sequence[PdfItem]) -> None:
        self.items = [(item, _normalised_index(item.text)[0]) for item in items]

    def places(self, needle: str, *, whole: bool, every: bool) -> list[_Place]:
        flat_needle, _ = _normalised_index(needle)
        if not flat_needle:
            return []
        out: list[_Place] = []
        for item, flat in self.items:
            if next(_found(flat, flat_needle, whole=whole), None) is not None:
                out.append(_Place(item.page - 1, (item.box,), exact=False))
                if not every or len(out) > MAX_SEARCHED:
                    return out
        return out


def _places_for(
    pages: _TextPages | _ScannedPages, finding: Finding, text: str | None
) -> list[_Place]:
    """Every place a finding should be marked, as ``docx_annotate._places_for`` decides it: the
    finding's own words wherever they stand, as whole words first; the correction, searched
    last, at its first place only. The places a finding names (``Finding.places``) are chosen
    among its quote's places when the PDF shows as many as the checked text did (and no more
    than a search reads, ``MAX_SEARCHED``)."""
    if finding.places is not None and finding.quote and finding.quote.strip():
        found = pages.places(finding.quote.strip(), whole=True, every=True)
        wanted, total = finding.places
        if found and len(found) == total <= MAX_SEARCHED:
            return [found[i] for i in wanted if 0 <= i < len(found)]
    offending = _offending(finding, text)
    for needle in _candidates(finding, text):
        every = needle in offending
        found = pages.places(needle, whole=True, every=every) or pages.places(
            needle, whole=False, every=every
        )
        if found:
            return found
    return []


def _set_text(annot: Any, key: str, value: str) -> None:
    """An annotation's text entry, as the UTF-16 units PDFium takes, ended with a zero unit: an
    array of the bindings' own wide character, which they pass as the pointer the call wants."""
    data = (_CONTROL.sub("", value) + "\x00").encode("utf-16-le")
    units = struct.unpack(f"<{len(data) // 2}H", data)
    buffer = (pdfium_c.FPDF_WCHAR * len(units))(*units)
    pdfium_c.FPDFAnnot_SetStringValue(annot, key.encode("ascii"), buffer)


def _highlight(page: Any, boxes: Sequence[Box], note: str, severity: str) -> bool:
    annot = pdfium_c.FPDFPage_CreateAnnot(page, pdfium_c.FPDF_ANNOT_HIGHLIGHT)
    if not annot:
        return False
    try:
        for left, bottom, right, top in boxes:
            quad = pdfium_c.FS_QUADPOINTSF(left, top, right, top, left, bottom, right, bottom)
            pdfium_c.FPDFAnnot_AppendAttachmentPoints(annot, quad)  # passed by reference
        rect = pdfium_c.FS_RECTF(
            min(b[0] for b in boxes),
            max(b[3] for b in boxes),
            max(b[2] for b in boxes),
            min(b[1] for b in boxes),
        )
        pdfium_c.FPDFAnnot_SetRect(annot, rect)
        red, green, blue = _COLOURS.get(severity, _COLOURS["info"])
        pdfium_c.FPDFAnnot_SetColor(
            annot, pdfium_c.FPDFANNOT_COLORTYPE_Color, red, green, blue, _OPACITY
        )
        _set_text(annot, "Contents", note)
        _set_text(annot, "T", AUTHOR)
        pdfium_c.FPDFAnnot_SetFlags(annot, pdfium_c.FPDF_ANNOT_FLAG_PRINT)
    finally:
        pdfium_c.FPDFPage_CloseAnnot(annot)
    return True


def _shown(page: Any) -> tuple[Box, int]:
    """What a page shows - its crop box, in the page's own space - and how far it is turned
    when shown, clockwise."""
    left, bottom, right, top = page.get_cropbox()
    return (float(left), float(bottom), float(right), float(top)), int(page.get_rotation()) % 360


def _to_page(page: Any, box: Box) -> Box:
    """A box on the page as it is shown - Docling's, its origin the shown page's bottom left - in
    the page's own space: the crop box's corner added and the turn undone (red team R6, R6-14)."""
    (left, bottom, right, top), turn = _shown(page)

    def point(x: float, y: float) -> tuple[float, float]:
        if turn == 90:
            return right - y, bottom + x
        if turn == 180:
            return right - x, top - y
        if turn == 270:
            return left + y, top - x
        return left + x, bottom + y

    (x0, y0), (x1, y1) = point(box[0], box[1]), point(box[2], box[3])
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _corner_note(page: Any) -> Any:
    """A note's box, 20 points square, 12 points in from the corner the page shows at its top
    left - inside its crop box, however it is turned (red team R6, R6-13)."""
    (left, bottom, right, top), turn = _shown(page)
    x, y = {
        90: (left + 12.0, bottom + 12.0),
        180: (right - 32.0, bottom + 12.0),
        270: (right - 32.0, top - 32.0),
    }.get(turn, (left + 12.0, top - 32.0))
    return pdfium_c.FS_RECTF(x, y + 20.0, x + 20.0, y)


def _sticky_note(page: Any, note: str) -> None:
    """A note on the page's top left corner: what no words of the act stand for."""
    annot = pdfium_c.FPDFPage_CreateAnnot(page, pdfium_c.FPDF_ANNOT_TEXT)
    if not annot:
        return
    try:
        pdfium_c.FPDFAnnot_SetRect(annot, _corner_note(page))
        _set_text(annot, "Contents", note)
        _set_text(annot, "T", AUTHOR)
        pdfium_c.FPDFAnnot_SetFlags(annot, pdfium_c.FPDF_ANNOT_FLAG_PRINT)
    finally:
        pdfium_c.FPDFPage_CloseAnnot(annot)


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

#: Names a PDF uses only to run something, reach out, or carry a file or a form: none belongs in
#: the annotated copy, whose pages are drawn anew and whose notes are highlights and text notes.
_ACTIVE_NAMES = frozenset(
    {
        "AA",
        "AF",
        "AcroForm",
        "EmbeddedFile",
        "EmbeddedFiles",
        "FileAttachment",
        "GoToE",
        "GoToR",
        "ImportData",
        "JS",
        "JavaScript",
        "Launch",
        "Movie",
        "OpenAction",
        "Rendition",
        "RichMedia",
        "Sound",
        "SubmitForm",
        "URI",
        "XFA",
    }
)
_PDF_SPACE = frozenset(b"\x00\t\n\x0c\r ")
_PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_LENGTH = re.compile(rb"[\x00\t\n\x0c\r ]*(\d+)(?![\x00\t\n\x0c\r ]+\d+[\x00\t\n\x0c\r ]+R)")


def active_names(data: bytes) -> set[str]:
    """The names in ``_ACTIVE_NAMES`` a PDF's objects use, read as PDF reads them: outside every
    string and every stream's data, an escaped name (``/J#53``) decoded. The backstop under the
    redrawing (``_redrawn``): what a page's resources might still carry - a form of its own with
    an attached file, say - is caught here, and the copy is not handed out."""
    found: set[str] = set()
    i, n = 0, len(data)
    length: int | None = None  # the last direct /Length read: the next stream's
    while i < n:
        c = data[i]
        if c == 0x25:  # % a comment, to the end of its line
            while i < n and data[i] not in (0x0A, 0x0D):
                i += 1
        elif c == 0x28:  # ( a literal string, balanced, with escapes
            depth, i = 1, i + 1
            while i < n and depth:
                ch = data[i]
                if ch == 0x5C:
                    i += 2
                    continue
                depth += (ch == 0x28) - (ch == 0x29)
                i += 1
        elif c == 0x3C:  # << a dictionary, or a <hex string>
            if data[i + 1 : i + 2] == b"<":
                i += 2
                continue
            end = data.find(b">", i)
            i = n if end < 0 else end + 1
        elif c == 0x2F:  # /name
            j = i + 1
            while j < n and data[j] not in _PDF_SPACE and data[j] not in _PDF_DELIMITERS:
                j += 1
            name = _NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), data[i + 1 : j])
            text = name.decode("latin-1")
            if text in _ACTIVE_NAMES:
                found.add(text)
            if text == "Length" and (m := _LENGTH.match(data, j)) is not None:
                length, j = int(m.group(1)), m.end()
            i = j
        elif data.startswith(b"stream", i) and data[i + 6 : i + 7] in (b"\r", b"\n"):
            start = i + 6 + (2 if data[i + 6 : i + 8] == b"\r\n" else 1)
            end = data.find(b"endstream", start + (length or 0))
            i = n if end < 0 else end + len(b"endstream")
            length = None
        else:
            i += 1
    return found


def annotate_pdf(
    source: Path,
    findings: Sequence[Finding],
    out_path: Path,
    *,
    text: str | None = None,
    summary: str | None = None,
    items: Sequence[PdfItem] = (),
) -> tuple[Path, list[Finding]]:
    """Write ``source`` to ``out_path`` with a highlight and a note for every place a finding
    marks, and a note on the first page for the summary and for the findings no words stand
    for. ``items`` are a scanned PDF's lines (``docling_items``), searched when the PDF has no
    text of its own. Returns the path and the findings that could not be placed."""
    if source.suffix.lower() != ".pdf":
        msg = f"PDF notes can only be written into a .pdf file, got {source.suffix!r}"
        raise UnsupportedDocumentError(msg)
    with PDFIUM_LOCK:  # pdfium is not thread-safe, not even across documents
        try:
            source_pdf = pdfium.PdfDocument(str(source))
        except (pdfium.PdfiumError, OSError) as e:
            msg = f"the file is not a PDF notes can be written into ({type(e).__name__})"
            raise UnsupportedDocumentError(msg) from e
        try:
            # the act's pages in a document of their own: what the act's file runs or carries
            # when it opens - an action, a script, an attached file, a form - stays behind
            pages = pdfium.PdfDocument.new()
            try:
                pages.import_pages(source_pdf)
                _cleared(pages)
                # and again, each page drawn anew from what it shows: the annotations the pages
                # no longer hold, and whatever else a page's own entries carry, stay behind
                pdf = _redrawn(pages)
                try:
                    return _annotate(pdf, findings, out_path, text, summary, items)
                finally:
                    pdf.close()
            finally:
                pages.close()
        finally:
            source_pdf.close()


def _page_matrix(page: Any) -> tuple[float, ...]:
    """PDFium's own transform from a page's space to the page as shown - the crop box (within
    the media box) turned upright, its corner at the origin: what a page drawn as a form
    (``page_as_xobject``) is drawn through."""
    ml, mb, mr, mt = page.get_mediabox()
    cl, cb, cr, ct = page.get_cropbox()
    left, bottom = max(ml, min(cl, cr)), max(mb, min(cb, ct))
    right, top = min(mr, max(cl, cr)), min(mt, max(cb, ct))
    if right <= left or top <= bottom:
        left, bottom, right, top = ml, mb, mr, mt
    return {
        90: (0.0, -1.0, 1.0, 0.0, -bottom, right),
        180: (-1.0, 0.0, 0.0, -1.0, right, top),
        270: (0.0, 1.0, -1.0, 0.0, top, -left),
    }.get(int(page.get_rotation()) % 360, (1.0, 0.0, 0.0, 1.0, -left, -bottom))


def _inverse(m: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = m
    det = a * d - b * c
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def _redrawn(pages: pdfium.PdfDocument) -> pdfium.PdfDocument:
    """A new document whose every page is a new page with the same boxes and turn, showing the
    page's content drawn as one form (``page_as_xobject``), placed where the page drew it. A
    page's dictionary is not copied - its annotations, their actions, its associated files, its
    metadata and whatever else it names stay behind (security review) - and a highlight sits on
    the same words as on the page itself."""
    pdf = pdfium.PdfDocument.new()
    try:
        for i in range(len(pages)):
            page = pages[i]
            try:
                form = pages.page_as_xobject(i, pdf)
                left, bottom, right, top = page.get_mediabox()
                new = pdf.new_page(right - left, top - bottom)
                new.set_mediabox(left, bottom, right, top)
                new.set_cropbox(*page.get_cropbox())
                new.set_rotation(int(page.get_rotation()) % 360)
                shown = form.as_pageobject()
                shown.transform(pdfium.PdfMatrix(*_inverse(_page_matrix(page))))
                new.insert_obj(shown)
                new.gen_content()
                new.close()
                form.close()
            finally:
                page.close()
    except BaseException:
        pdf.close()
        raise
    return pdf


_PAGE_ACTIONS = (pdfium_c.FPDFPAGE_AACTION_OPEN, pdfium_c.FPDFPAGE_AACTION_CLOSE)


def _cleared(pdf: pdfium.PdfDocument) -> None:
    """Each page's own annotations - links, form fields, notes - drawn into the page, their
    actions gone with them; ``UnsupportedDocumentError`` for a page that runs an action of its
    own on opening or closing. (The document's own scripts and attached files are no page's:
    importing the pages into a new document left them behind.)"""
    for i in range(len(pdf)):
        page = pdf[i]
        try:
            if any(pdfium_c.FPDF_GetPageAAction(page, kind) for kind in _PAGE_ACTIONS):
                msg = (
                    "the PDF runs an action when a page is opened or closed: no annotated copy "
                    "of it is made"
                )
                raise UnsupportedDocumentError(msg)
            flattened = pdfium_c.FPDFPage_Flatten(page, pdfium_c.FLAT_NORMALDISPLAY)
        finally:
            page.close()
        page = pdf[i]  # a flattened page is read again
        try:
            left = pdfium_c.FPDFPage_GetAnnotCount(page)
        finally:
            page.close()
        if flattened == pdfium_c.FLATTEN_FAIL or left:
            msg = "the PDF's own annotations could not be drawn into its pages"
            raise UnsupportedDocumentError(msg)


def _annotate(
    pdf: pdfium.PdfDocument,
    findings: Sequence[Finding],
    out_path: Path,
    text: str | None,
    summary: str | None,
    items: Sequence[PdfItem],
) -> tuple[Path, list[Finding]]:
    text_pages = _TextPages(pdf)
    try:
        pages: _TextPages | _ScannedPages = (
            text_pages if text_pages.characters or not items else _ScannedPages(items)
        )
        # one highlight a place, its note every finding there, the gravest one's colour
        notes: dict[tuple[int, tuple[tuple[float, ...], ...]], tuple[_Place, list[Finding]]] = {}
        unanchored: list[Finding] = []
        cut_short: list[str] = []
        marked = 0
        for finding in findings:
            places = _places_for(pages, finding, text)
            # as many places as a Word file is marked in (``docx_annotate.MAX_PLACES``)
            room = max(0, min(MAX_PLACES_PER_FINDING, MAX_PLACES - marked))
            if len(places) > room:
                cut_short.append(_cut_short_note(finding, room, len(places)))
                places = places[:room]
            if not places:
                unanchored.append(finding)
                continue
            marked += len(places)
            for place in places:
                key = (place.page, tuple(tuple(round(v, 1) for v in b) for b in place.boxes))
                notes.setdefault(key, (place, []))[1].append(finding)
    finally:
        text_pages.close()
    opened: dict[int, Any] = {}  # each page loaded once, however many places it holds
    for place, here in notes.values():
        note = "\n\n".join(comment_text(f) for f in here)
        if place.page not in opened:
            opened[place.page] = pdf[place.page]
        page = opened[place.page]
        boxes = place.boxes
        if not place.exact:
            # a scanned page's line is Docling's, on the page as shown
            boxes = tuple(_to_page(page, box) for box in boxes)
            note = f"{note}\n\n{_SCANNED_NOTE}"
        severity = min((f.severity for f in here), key=lambda s: _SEVERITY_ORDER[s])
        _highlight(page, boxes, note, severity)
    extra = [*([summary] if summary else []), *cut_short, *(comment_text(f) for f in unanchored)]
    if extra and len(pdf):
        _sticky_note(opened.get(0) or pdf[0], "\n\n".join(extra))
    pdf.save(str(out_path))
    if active := active_names(out_path.read_bytes()):
        out_path.unlink(missing_ok=True)
        log.info("pdf_annotation_refused", names=sorted(active))  # names from a fixed set
        msg = (
            "the PDF's pages carry an action, a script or an attached file: no annotated copy "
            "of it is made"
        )
        raise UnsupportedDocumentError(msg)
    return out_path, unanchored

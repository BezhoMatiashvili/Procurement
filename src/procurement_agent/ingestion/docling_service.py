"""Docling conversion for PDF and DOCX, configured for CPU-only, offline operation."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import threading
from importlib.metadata import PackageNotFoundError, version
from itertools import pairwise
from pathlib import Path
from typing import Any

from docling.backend.docling_parse_backend import (
    ThreadedDoclingParseDocumentBackend,
    ThreadedDoclingParsePageBackend,
)
from docling.backend.msword_backend import MsWordDocumentBackend
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableStructureOptions,
    TesseractCliOcrOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.page import BoundingRectangle, TextDirection

from procurement_agent.config import Settings
from procurement_agent.ingestion.text_limits import compact_document, text_size
from procurement_agent.security.audit import log
from procurement_agent.security.safe_errors import ContentFree


class ConversionError(ContentFree, RuntimeError):
    """Raised with a message made of Docling's status and component names, never its error text."""


class WholeParagraphWordBackend(MsWordDocumentBackend):
    """Docling's Word reader, with each paragraph's text read whole.

    Docling splits a paragraph wherever the character formatting changes (bold, italic,
    underline, a hyperlink) into pieces it strips of their edge spaces, and the chunker writes
    the pieces one to a line. A correct act with one italic "50" inside "3 450", or a surname
    whose last letters are bold, then read "3 4\\n50" and "ბარამ\\nიძე": a fee of 50 and a
    misspelled director (red team R3, AOA-R3-13), and the space a run began with was lost for
    good. Nothing downstream reads the formatting (the serializer writes plain text), so a
    paragraph is one piece of text, exactly as Word shows it; an empty one stays as Docling
    keeps it.
    """

    def _get_paragraph_elements(self, paragraph: Any) -> Any:
        elements = super()._get_paragraph_elements(paragraph)
        if len(elements) <= 1:
            return elements
        text = self._get_paragraph_text(paragraph).strip()
        return [(text, elements[0][1], None)]


def _lines_of(cells: list[Any]) -> list[list[int]]:
    """The cells (positions in ``cells``) on each line: cells whose heights overlap by half the
    lower one, from the top down."""
    boxes = [cell.rect.to_bounding_box() for cell in cells]
    spans = [(min(b.t, b.b), max(b.t, b.b)) for b in boxes]
    lines: list[list[int]] = []
    top = bottom = 0.0
    for i in sorted(range(len(cells)), key=lambda i: (spans[i][0] + spans[i][1]) / 2):
        a, b = spans[i]
        if lines and min(b, bottom) - max(a, top) >= 0.5 * min(b - a, bottom - top):
            lines[-1].append(i)
            top, bottom = min(top, a), max(bottom, b)
        else:
            lines.append([i])
            top, bottom = a, b
    return lines


#: How wide a gap between two words, against the line's height, is a space between them.
_SPACE_GAP = 0.15


def mend_interleaved(textlines: list[Any], words: list[Any]) -> list[Any]:
    """A page's text cells, those a line's runs were written into across each other, or right
    up against each other, made one.

    A writer that falls back to another font for a line's digits and punctuation may write the
    letters' run whole and the others' after it: Docling then reads "ქ თბილისი" as one cell and
    "." as another lying inside it, and no order of the two reads "ქ. თბილისი". Two runs that
    touch it joins with a space ("ბათუმი ,"), or, when the first ends in a hyphen, as a word
    broken over a line - "01.07.2026-" and "დან" became "01.07.2026დან". Cells of one line that
    overlap or touch are made one cell, its text the line's words there (``words``, which lie
    each where it stands) left to right, a space between two words that are apart. A page
    whose cells are apart - a page written one font to a line - is returned as it was."""
    if len(textlines) < 2:
        return textlines
    merged: dict[int, Any] = {}
    gone: set[int] = set()
    for line in _lines_of(textlines):
        if len(line) < 2:
            continue
        boxes = {i: textlines[i].rect.to_bounding_box() for i in line}
        height = max(max(b.t, b.b) for b in boxes.values()) - min(
            min(b.t, b.b) for b in boxes.values()
        )
        clump: list[int] = []
        right = -1.0
        for i in [*sorted(line, key=lambda i: boxes[i].l), None]:
            # across each other, or touching: no space between them as the line shows it
            if i is not None and clump and boxes[i].l < right + _SPACE_GAP * height:
                clump.append(i)
                right = max(right, boxes[i].r)
                continue
            if len(clump) > 1:
                first = min(clump, key=lambda k: textlines[k].index)
                merged[first] = _rebuilt(textlines, clump, first, words)
                gone.update(k for k in clump if k != first)
            if i is not None:
                clump, right = [i], boxes[i].r
    if not merged:
        return textlines
    return [merged.get(i, cell) for i, cell in enumerate(textlines) if i not in gone]


def _rebuilt(textlines: list[Any], clump: list[int], first: int, words: list[Any]) -> Any:
    """The one cell the overlapping cells ``clump`` of a line stand for (``mend_interleaved``)."""
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
            text += (" " if gap > _SPACE_GAP * (bottom - top) else "") + word
    else:  # no words read: the cells' own texts, left to right
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


def in_line_order(cells: list[Any]) -> None:
    """A PDF page's text cells numbered, and listed, so that each line reads left to right.

    A writer that draws a line's text in more than one font - the Georgian letters in one, the
    digits and the punctuation in another it falls back to - may write the runs out of their
    order on the line, and Docling reads them in the order written: "2026 21 წლის აგვისტო" for
    "2026 წლის 21 აგვისტო" (Test Set 4's PDF acts, written by LibreOffice). A line here is the
    cells whose heights overlap by half the lower one; it keeps the place of its first cell in
    the order the page was written, and its cells follow one another left to right. A page
    written in order is left as it is; a line written right to left is not touched."""
    if len(cells) < 2:
        return
    boxes = [cell.rect.to_bounding_box() for cell in cells]
    lines = _lines_of(cells)
    lines.sort(key=lambda line: min(cells[i].index for i in line))
    slots = sorted(cell.index for cell in cells)
    order: list[int] = []
    for line in lines:
        rtl = any(cells[i].text_direction == TextDirection.RIGHT_TO_LEFT for i in line)
        order += line if rtl else sorted(line, key=lambda i: (boxes[i].l, cells[i].index))
    ordered = [cells[i] for i in order]
    for cell, slot in zip(ordered, slots, strict=True):
        cell.index = slot
    cells[:] = ordered


class _LineOrderedPage(ThreadedDoclingParsePageBackend):
    """A page whose text reads left to right on every line: its text cells written across each
    other made one (``mend_interleaved``), and its text and word cells - the words a table's
    cells are read from - in line order (``in_line_order``)."""

    def __init__(self, result: Any, rendered: bool = True) -> None:
        super().__init__(result, rendered=rendered)
        self._ordered = False

    def get_segmented_page(self) -> Any:
        page = super().get_segmented_page()
        if page is not None and not self._ordered:
            page.textline_cells[:] = mend_interleaved(page.textline_cells, page.word_cells)
            in_line_order(page.textline_cells)
            in_line_order(page.word_cells)
            self._ordered = True
        return page


class LineOrderedPdfBackend(ThreadedDoclingParseDocumentBackend):
    """Docling's PDF reader, each page's lines read left to right (``in_line_order``)."""

    def iter_pages(self) -> Any:
        for page in super().iter_pages():
            yield _LineOrderedPage(page._result, rendered=page._rendered)


#: Bumped whenever this module changes what a conversion yields for the same file, so a cache
#: of parses (``conversion_fingerprint``) never serves one made the old way.
_CONVERSION_REVISION = "whole-paragraphs-1+pdf-lines-3"

_lock = threading.Lock()
#: One converter per distinct configuration; see ``get_converter``.
_converters: dict[tuple[object, ...], DocumentConverter] = {}
_ocr_available: bool | None = None


def ocr_available(settings: Settings) -> bool:
    global _ocr_available
    if _ocr_available is None:
        _ocr_available = shutil.which("tesseract") is not None
        if not _ocr_available:
            log.warning("tesseract_missing", detail="OCR disabled; scanned PDFs will yield no text")
    return _ocr_available


def _build(settings: Settings) -> DocumentConverter:
    pdf = PdfPipelineOptions(
        artifacts_path=settings.docling_artifacts_path,
        enable_remote_services=False,
        allow_external_plugins=False,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(do_cell_matching=True),
        do_ocr=ocr_available(settings),
        ocr_options=TesseractCliOcrOptions(lang=list(settings.ocr_langs)),
        accelerator_options=AcceleratorOptions(
            num_threads=settings.docling_num_threads, device=AcceleratorDevice.CPU
        ),
        generate_page_images=False,
        document_timeout=float(settings.docling_timeout_s),
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf, backend=LineOrderedPdfBackend),
            InputFormat.DOCX: WordFormatOption(backend=WholeParagraphWordBackend),
        },
    )


def _converter_key(settings: Settings) -> tuple[object, ...]:
    """Every setting ``_build`` reads. Two settings objects that agree here convert alike."""
    return (
        str(settings.docling_artifacts_path),
        tuple(settings.ocr_langs),
        ocr_available(settings),
        settings.docling_num_threads,
        settings.docling_timeout_s,
    )


def get_converter(settings: Settings) -> DocumentConverter:
    """One converter per configuration; model loading is expensive and thread-safe once built.

    This used to be a single process-wide instance built from whichever settings asked first,
    so a later call with different OCR languages or a different timeout silently got the old
    configuration. Keyed by the settings that shape the pipeline, a changed setting now gets
    its own converter and an unchanged one still reuses the loaded models.
    """
    key = _converter_key(settings)
    with _lock:
        converter = _converters.get(key)
        if converter is None:
            converter = _converters[key] = _build(settings)
        return converter


def conversion_fingerprint(settings: Settings) -> str:
    """A short hash of everything besides the file itself that changes Docling's output.

    Used to key caches of parsed documents: the Docling version, whether OCR can run at all,
    the OCR languages and the model artifacts. A cache keyed by the file alone served a parse
    made without OCR (or with other languages, or another Docling) as if it were current.
    """
    try:
        docling_version = version("docling")
    except PackageNotFoundError:  # pragma: no cover - docling is a hard dependency
        docling_version = "unknown"
    parts = [
        docling_version,
        _CONVERSION_REVISION,
        str(ocr_available(settings)),
        ",".join(settings.ocr_langs),
        str(settings.docling_artifacts_path),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def convert(path: Path, settings: Settings) -> ConversionResult:
    conv = get_converter(settings)
    try:
        result = conv.convert(
            path,
            raises_on_error=False,
            max_num_pages=settings.max_pages,
            max_file_size=settings.max_file_bytes,
        )
    except Exception as e:  # docling can raise from native backends
        msg = f"conversion crashed: {type(e).__name__}"
        raise ConversionError(msg) from e
    n = text_size(result.document)
    limit = settings.max_text_chars
    if n > limit:
        msg = f"the document holds {n} characters of text, more than the limit of {limit}"
        raise ConversionError(msg)
    if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        # Docling's error_message may quote the page it choked on: the status and the components
        # that failed say enough, and are all that is kept.
        parts = sorted({f"{e.component_type.value}/{e.module_name}" for e in result.errors})
        msg = f"conversion failed: {result.status.value} ({len(result.errors)} error(s)"
        raise ConversionError(msg + (f" in {', '.join(parts[:3])})" if parts else ")"))
    return result


def save_document(doc: DoclingDocument, out_dir: Path) -> Path:
    """Write ``docling.json`` atomically: a crash mid-write never leaves a truncated file.

    Readers treat the file's existence as "this document is parsed", so a half-written one
    would be loaded later and fail - or, in the evaluation cache, be trusted forever. The JSON
    is written to a private temporary file in the same directory and renamed into place.
    """
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = out_dir / "docling.json"
    fd, tmp_name = tempfile.mkstemp(prefix=".docling.", suffix=".json.tmp", dir=out_dir)
    os.close(fd)  # mkstemp creates the file 0600; docling reopens it by name
    tmp = Path(tmp_name)
    try:
        doc.save_as_json(tmp)
        tmp.chmod(0o600)
        tmp.replace(target)  # atomic within one directory
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def load_document(path: Path) -> DoclingDocument:
    """A stored parse, with runs of separators cut to what the patterns can read in linear time
    (``text_limits``). Applied on every load, so a parse stored before the limit existed is read
    the same way as one stored after."""
    doc = DoclingDocument.load_from_json(path)
    compact_document(doc)
    return doc

"""Docling conversion for PDF and DOCX, configured for CPU-only, offline operation."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

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
from docling_core.types.doc.document import DoclingDocument

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


#: Bumped whenever this module changes what a conversion yields for the same file, so a cache
#: of parses (``conversion_fingerprint``) never serves one made the old way.
_CONVERSION_REVISION = "whole-paragraphs-1"

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
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf),
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

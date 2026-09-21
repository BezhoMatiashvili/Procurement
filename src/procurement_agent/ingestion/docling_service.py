"""Docling conversion for PDF and DOCX, configured for CPU-only, offline operation."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

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
from procurement_agent.security.audit import log


class ConversionError(RuntimeError):
    pass


_lock = threading.Lock()
_converter: DocumentConverter | None = None
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
            InputFormat.DOCX: WordFormatOption(),
        },
    )


def get_converter(settings: Settings) -> DocumentConverter:
    """Process-wide singleton; model loading is expensive and thread-safe once built."""
    global _converter
    with _lock:
        if _converter is None:
            _converter = _build(settings)
        return _converter


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
    if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        errs = "; ".join(str(e.error_message) for e in result.errors[:3]) or str(result.status)
        msg = f"conversion failed: {errs}"
        raise ConversionError(msg)
    return result


def save_document(doc: DoclingDocument, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = out_dir / "docling.json"
    doc.save_as_json(target)
    target.chmod(0o600)
    return target


def load_document(path: Path) -> DoclingDocument:
    return DoclingDocument.load_from_json(path)

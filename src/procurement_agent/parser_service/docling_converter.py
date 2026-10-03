"""The converter the parser container serves: Docling, through the web process's own functions.

Nothing here is a second implementation. ``docling_service.convert`` and ``save_document`` are the
two calls ``jobs/worker.py`` makes for an inline parse, and this makes the same two in the same
order; what comes back over the socket is the file ``save_document`` wrote, byte for byte, so the
document the web process stores is the one it would have made itself.
"""

from __future__ import annotations

from pathlib import Path

from docling.datamodel.base_models import InputFormat

from procurement_agent.config import Settings
from procurement_agent.ingestion import docling_service
from procurement_agent.parser_service.server import Converted, Converter

__all__ = ["load"]


def load(settings: Settings) -> Converter:
    """Build the converter and load both pipelines' models now.

    Docling loads them on the first conversion otherwise, which would be inside the kill switch's
    window: a parser restarted by the kill would spend its first document's deadline on loading.
    """
    converter = docling_service.get_converter(settings)
    for fmt in (InputFormat.PDF, InputFormat.DOCX):
        converter.initialize_pipeline(fmt)

    def convert(source: Path, kind: str) -> Converted:
        result = docling_service.convert(source, settings)
        saved = docling_service.save_document(result.document, source.parent / "result")
        return Converted(
            pages=len(result.document.pages),
            status=result.status.value,
            document=saved.read_bytes(),
        )

    return convert

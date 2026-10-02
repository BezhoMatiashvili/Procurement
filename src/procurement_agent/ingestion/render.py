"""Render a PDF page to PNG with highlighted bounding boxes (for the 'show source' UI)."""

from __future__ import annotations

import io
import math
from pathlib import Path

import pypdfium2 as pdfium
from PIL import ImageDraw

from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK
from procurement_agent.security.safe_errors import ContentFree

Box = tuple[float, float, float, float]

#: Most pixels one rendered page may have. The size comes from the PDF itself: a page 14 400
#: points on a side (the PDF limit) at the 1.5 scale is a 466-megapixel bitmap, which Pillow
#: then copies twice more. 25 megapixels is a 5 000 x 5 000 image - far more than the "show
#: source" view needs.
MAX_PIXELS = 25_000_000
#: A page that would have to be drawn smaller than this is refused instead of shown as a speck.
_MIN_SCALE = 0.1
#: Most boxes drawn on one page.
MAX_BOXES = 20


class PageTooLargeError(ContentFree, ValueError):
    """The page is too large to be rendered as an image."""


def _fit_scale(width_pt: float, height_pt: float, wanted: float) -> float:
    """``wanted``, or less when the page would pass ``MAX_PIXELS`` at that scale."""
    # a finite product of two positive sides: NaN, 0, a negative and an infinite side all fail it
    if not (width_pt > 0 and height_pt > 0 and math.isfinite(width_pt * height_pt)):
        msg = "page has no usable size"
        raise PageTooLargeError(msg)
    scale = min(wanted, math.sqrt(MAX_PIXELS / (width_pt * height_pt)))
    if scale < _MIN_SCALE:
        msg = "page is too large to render"
        raise PageTooLargeError(msg)
    return scale


def _pixel_rect(box: Box, scale: float, size: tuple[int, int]) -> Box | None:
    """The box in pixels, pulled inside the image; None when it is not a finite box or lies
    outside the image altogether.

    The numbers come from the caller's query string. Pillow draws a rectangle by walking its
    extent, with the GIL held, so one with an edge at 7e8 stopped the whole process for five
    seconds, and a NaN made the order of the edges undefined. Clamped to the page the cost is
    bounded by the page, whatever was asked for.
    """
    if not all(math.isfinite(v) for v in box):
        return None
    left, right = sorted((box[0] * scale, box[2] * scale))
    top, bottom = sorted((box[1] * scale, box[3] * scale))
    width, height = size
    if right < 0 or left > width or bottom < 0 or top > height:
        return None
    return (max(left, 0.0), max(top, 0.0), min(right, float(width)), min(bottom, float(height)))


def render_page(pdf_path: Path, page_no: int, boxes: list[Box], *, scale: float = 1.5) -> bytes:
    with PDFIUM_LOCK:  # pdfium is not thread-safe, not even across documents
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            if page_no < 1 or page_no > len(pdf):
                msg = "page out of range"
                raise ValueError(msg)
            page = pdf[page_no - 1]
            try:
                width_pt, height_pt = page.get_size()
                scale = _fit_scale(width_pt, height_pt, scale)
                img = page.render(scale=scale).to_pil().convert("RGBA")
            finally:
                page.close()
        finally:
            pdf.close()

    rects = [r for box in boxes[:MAX_BOXES] if (r := _pixel_rect(box, scale, img.size)) is not None]
    if rects:
        overlay = img.copy()
        draw = ImageDraw.Draw(overlay, "RGBA")
        for rect in rects:
            draw.rectangle(rect, fill=(255, 200, 0, 70), outline=(230, 80, 0, 255), width=3)
        img = overlay

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()

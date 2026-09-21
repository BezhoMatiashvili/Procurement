"""Render a PDF page to PNG with highlighted bounding boxes (for the 'show source' UI)."""

from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import ImageDraw

Box = tuple[float, float, float, float]


def render_page(pdf_path: Path, page_no: int, boxes: list[Box], *, scale: float = 1.5) -> bytes:
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        if page_no < 1 or page_no > len(pdf):
            msg = "page out of range"
            raise ValueError(msg)
        page = pdf[page_no - 1]
        try:
            img = page.render(scale=scale).to_pil().convert("RGBA")
        finally:
            page.close()
    finally:
        pdf.close()

    if boxes:
        overlay = img.copy()
        draw = ImageDraw.Draw(overlay, "RGBA")
        for left, top, right, bottom in boxes:
            rect = (left * scale, top * scale, right * scale, bottom * scale)
            draw.rectangle(rect, fill=(255, 200, 0, 70), outline=(230, 80, 0, 255), width=3)
        img = overlay

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()

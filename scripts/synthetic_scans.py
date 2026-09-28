#!/usr/bin/env python
"""Scanned-looking PDFs made from Word files whose text is known.

    uv run python scripts/synthetic_scans.py "samples/Test Set 2/AOA and Contract" \\
        --out samples/synthetic_scans/test-set-2 --seed 1

Each ``.docx`` is printed to PDF by LibreOffice, rasterised with ``pdftoppm`` and put through what
a scanner does to a page - a slight tilt, blur, grain, uneven contrast and JPEG compression -
then saved as a PDF of images with no text layer, which is what the client's scans are. The
Word file's own text is the ground truth, so these pages measure the OCR and the OCR-aware
checks without leaning on the client's seven scans; the degradation is seeded, so a run can be
repeated. Needs ``soffice`` and ``pdftoppm`` on the host; OCR itself runs in the image
(``scripts/ocr_confusions.py``). The output holds the documents' text: keep it under
``samples/`` (git-ignored).
"""

from __future__ import annotations

import argparse
import io
import random
import re
import shutil
import subprocess  # nosec B404 - fixed local tools, no shell, arguments are paths
import tempfile
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

DPI = 200


def _page_images(pdf: Path, work: Path) -> list[Path]:
    prefix = work / pdf.stem
    subprocess.run(  # nosec B603 B607 - pdftoppm from PATH with a file argument
        ["pdftoppm", "-r", str(DPI), "-gray", "-png", str(pdf), str(prefix)],
        check=True,
        capture_output=True,
    )
    return sorted(work.glob(f"{pdf.stem}*.png"))


def _degrade(image: Image.Image, rng: random.Random) -> Image.Image:
    """A page as a flatbed scanner returns it: tilted, soft, grainy, compressed."""
    page = image.convert("L")
    page = page.rotate(rng.uniform(-0.8, 0.8), resample=Image.Resampling.BICUBIC, fillcolor=255)
    page = page.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 0.9)))
    pixels = np.asarray(page, dtype=np.float32)
    gain, offset = rng.uniform(0.85, 1.0), rng.uniform(0.0, 18.0)
    grain = np.random.default_rng(rng.randrange(2**32))
    noise = grain.normal(0.0, rng.uniform(4.0, 9.0), pixels.shape)
    pixels = np.clip(pixels * gain + offset + noise, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buf, format="JPEG", quality=rng.randint(55, 75))
    buf.seek(0)
    return Image.open(buf).convert("L")


_RFONTS = re.compile(r"<w:rFonts\b[^>]*/>")
_LANG = re.compile(r"<w:lang\b[^>]*/>")
_ATTR = re.compile(r'w:(ascii|hAnsi|cs)="([^"]*)"')


def _same_font(m: re.Match[str]) -> str:
    """An ``rFonts`` element whose complex-script font is its Latin one."""
    element = m.group(0)
    fonts = dict(_ATTR.findall(element))
    latin = fonts.get("ascii") or fonts.get("hAnsi")
    if not latin or "cs" not in fonts:
        return element
    return element.replace(f'w:cs="{fonts["cs"]}"', f'w:cs="{latin}"')


def _with_fonts(docx: Path, work: Path) -> Path:
    """A copy whose runs print their digits and Latin letters. LibreOffice picks a run's font
    by its language, and a Latin code tagged Georgian ("TBCBGE22", lang ka-GE) printed as empty
    boxes - text no OCR can be scored on. The language tags go, and a complex-script font that
    differs from the Latin one is made that one; the text and its layout do not change."""
    out = work / docx.name
    with zipfile.ZipFile(docx) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                text = _RFONTS.sub(_same_font, data.decode("utf-8"))
                data = _LANG.sub("", text).encode("utf-8")
            dst.writestr(item, data)
    return out


def scan(docx: Path, out_dir: Path, rng: random.Random) -> Path:
    with tempfile.TemporaryDirectory(prefix="synthetic-scan-") as tmp:
        work = Path(tmp)
        (work / "src").mkdir()
        source = _with_fonts(docx, work / "src")
        subprocess.run(  # nosec B603 B607 - LibreOffice from PATH, headless, paths only
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(work), str(source)],
            check=True,
            capture_output=True,
        )
        printed = work / f"{docx.stem}.pdf"
        pages = [_degrade(Image.open(p), rng) for p in _page_images(printed, work)]
        out = out_dir / f"{docx.stem}.scan.pdf"
        pages[0].save(out, save_all=True, append_images=pages[1:], resolution=DPI)
        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="a folder of .docx files")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    for tool in ("soffice", "pdftoppm"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} is not on PATH")
    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)  # noqa: S311 - a repeatable page degradation, not a secret
    for docx in sorted(args.folder.glob("*.docx")):
        out = scan(docx, args.out, rng)
        # the ground truth travels with the scan, under the same stem
        shutil.copyfile(docx, args.out / docx.name)
        print(f"{docx.name} -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

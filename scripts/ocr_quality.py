#!/usr/bin/env python
"""How well the scanned documents read, per OCR configuration (docs/GOLD_SPEC.md section 6).

Runs where Tesseract is - inside the image, with the working tree mounted read-only:

    docker run --rm --entrypoint python \\
        -v "$PWD/src:/app/src:ro" -v "$PWD/scripts:/app/scripts:ro" \\
        -v "$PWD/samples:/app/samples:ro" -v "$PWD/models:/models:ro" -v "$OUT:/out" \\
        procurement-agent:0.1.0 /app/scripts/ocr_quality.py --mode full_page --scale 4.17 \\
        --out /out/ocr-full-300.json

Every scan (``samples/*/AOA and Contract/**/*.pdf``) is converted exactly as the product does -
Docling, then the chunker - under the OCR configuration given, and matched to its born-digital
twin (the ``*.docx`` in the same set) by text similarity, never by file name. Per scan it
reports:

* the character error rate against the twin, after folding spaces and quotes (layout order
  differs between a table in Word and its scan, so this is an upper bound, and meant for
  comparing configurations);
* which of the twin's codes - IBANs, bank codes, identification and cadastral codes, dates in
  figures - the OCR text carries character for character, which is what the checks compare;
* for a scanned act, the AOA check against the scanned contract and against the Word one: the
  rules it reports, beside the rules the twin's gold expects.

The JSON holds the documents' codes and findings, so it goes to a git-ignored directory.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402
from docling.datamodel.accelerator_options import (  # noqa: E402
    AcceleratorDevice,
    AcceleratorOptions,
)
from docling.datamodel.base_models import InputFormat  # noqa: E402
from docling.datamodel.pipeline_options import (  # noqa: E402
    OcrMode,
    PdfPipelineOptions,
    TableStructureOptions,
    TesseractCliOcrOptions,
)
from docling.document_converter import (  # noqa: E402
    DocumentConverter,
    PdfFormatOption,
    WordFormatOption,
)

from procurement_agent.checks.aoa import check_aoa  # noqa: E402
from procurement_agent.extraction.pipeline import chunks_text  # noqa: E402
from procurement_agent.ingestion.chunker import chunk_docling_document  # noqa: E402

SAMPLES = ROOT / "samples"
HELD_OUT = frozenset({"Test Set 3"})

#: The codes a check compares, as documents write them.
_CODES = {
    "iban": re.compile(r"\bGE\d{2}[A-Z]{2}\d{16}\b"),
    "bic": re.compile(r"\b[A-Z]{4}GE\d{2}\b"),
    "id": re.compile(r"(?<!\d)(?:\d{9}|\d{11})(?!\d)"),
    "cadastral": re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}(?:\.\d{2,3})?\b"),
    "date": re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{4}\b"),
}
_QUOTES = dict.fromkeys((0x201E, 0x201C, 0x201D, 0x22, 0x27, 0x2018, 0x2019, 0xAB, 0xBB), None)


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).translate(_QUOTES)).strip()


def _converter(mode: str, scale: float, psm: int | None, tessdata: str | None) -> DocumentConverter:
    ocr = TesseractCliOcrOptions(
        lang=["kat", "eng"],
        mode=OcrMode.FULL_PAGE if mode == "full_page" else OcrMode.DEFAULT,
        scale=scale,
        psm=psm,
        path=tessdata,
    )
    pdf = PdfPipelineOptions(
        artifacts_path="/models/docling",
        enable_remote_services=False,
        allow_external_plugins=False,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(do_cell_matching=True),
        do_ocr=True,
        ocr_options=ocr,
        accelerator_options=AcceleratorOptions(num_threads=8, device=AcceleratorDevice.CPU),
        generate_page_images=False,
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf),
            InputFormat.DOCX: WordFormatOption(),
        },
    )


def _text(converter: DocumentConverter, path: Path) -> str:
    doc = converter.convert(path).document
    return chunks_text(chunk_docling_document(doc, doc_id="x"))


def _cer(truth: str, ocr: str) -> float:
    """Edit operations over the truth's length, from difflib's alignment: an upper bound on the
    true edit distance, and stable enough to rank configurations."""
    a, b = _fold(truth), _fold(ocr)
    if not a:
        return 1.0
    edits = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op != "equal":
            edits += max(i2 - i1, j2 - j1)
    return edits / len(a)


def _codes(text: str) -> dict[str, list[str]]:
    return {k: sorted(set(p.findall(text))) for k, p in _CODES.items()}


def _similarity(a: str, b: str) -> float:
    """Character 3-gram overlap: cheap, and insensitive to where a table's cells land."""
    ga = {a[i : i + 3] for i in range(len(a) - 2)}
    gb = {b[i : i + 3] for i in range(len(b) - 2)}
    return len(ga & gb) / max(1, len(ga | gb))


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w{3,}", _fold(text)))


def _twin_of(scan: str, twins: dict[Path, str]) -> Path:
    """The twin the scan is, by a pairwise vote on the words that tell two twins apart.

    Acts of one template differ in a few words - a name, an amount, a typo - so overall likeness
    ranks two of them alike, and a fuzzy word match reads a planted typo as the right word. For
    each two twins, the scan sides with the one whose words the other lacks it reads more of,
    exactly; the twin that wins most such contests is the one. A scan that reads neither side
    better (the pair differs only where OCR lost both) casts no vote."""
    words = {t: _words(text) for t, text in twins.items()}
    seen = _words(scan)
    wins = dict.fromkeys(twins, 0)
    names = list(twins)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            only_a, only_b = words[a] - words[b], words[b] - words[a]
            share_a = len(only_a & seen) / len(only_a) if only_a else 0.0
            share_b = len(only_b & seen) / len(only_b) if only_b else 0.0
            if share_a > share_b:
                wins[a] += 1
            elif share_b > share_a:
                wins[b] += 1
    return max(twins, key=lambda t: (wins[t], _similarity(_fold(scan), _fold(twins[t]))))


def _gold_rules(twin: Path) -> list[str] | None:
    """The rules the client_v2 gold expects for the act ``twin`` (None without a gold file)."""
    for path in sorted((SAMPLES / "gold_v2" / "client").glob("aoa_*.yaml")):
        gold = yaml.safe_load(path.read_text(encoding="utf-8"))
        if Path(gold.get("document", "")).name == twin.name:
            return sorted({f["rule"] for f in gold.get("findings", [])})
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["default", "full_page"], default="default")
    parser.add_argument("--scale", type=float, default=3.0, help="72 dpi times this")
    parser.add_argument("--psm", type=int, default=None)
    parser.add_argument("--tessdata", default=None, help="a tessdata directory (e.g. _best)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dump", type=Path, default=None, help="write each text to this dir")
    args = parser.parse_args(argv)

    converter = _converter(args.mode, args.scale, args.psm, args.tessdata)
    config = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    report: dict[str, Any] = {"config": config, "scans": []}
    for folder in sorted(SAMPLES.glob("*/AOA and Contract")):
        if folder.parent.name in HELD_OUT:
            continue
        twins = {p: _text(converter, p) for p in sorted(folder.glob("*.docx"))}
        contract_docx = next((p for p in twins if "ontract" in p.name), None)
        scans: dict[Path, str] = {}
        seconds: dict[Path, float] = {}
        for pdf in sorted(folder.rglob("*.pdf")):
            start = time.monotonic()
            scans[pdf] = _text(converter, pdf)
            seconds[pdf] = time.monotonic() - start
        pairs = {pdf: _twin_of(text, twins) for pdf, text in scans.items()}
        contract_scan = next((p for p, t in pairs.items() if t == contract_docx), None)
        for pdf, text in scans.items():
            twin = pairs[pdf]
            truth = twins[twin]
            want, got = _codes(truth), _codes(text)
            entry: dict[str, Any] = {
                "scan": pdf.name,
                "twin": twin.name,
                "similarity": round(_similarity(_fold(text), _fold(truth)), 3),
                "seconds": round(seconds[pdf], 1),
                "cer": round(_cer(truth, text), 4),
                "codes_read": {
                    k: f"{len(set(v) & set(got[k]))}/{len(v)}" for k, v in want.items() if v
                },
                "codes_missed": {k: sorted(set(v) - set(got[k])) for k, v in want.items()},
            }
            if twin != contract_docx and contract_docx is not None:
                entry["gold_rules"] = _gold_rules(twin)
                for label, contract_text in (
                    ("vs_docx_contract", twins[contract_docx]),
                    ("vs_scanned_contract", scans.get(contract_scan) if contract_scan else None),
                ):
                    if contract_text is None:
                        continue
                    rep = check_aoa(aoa_text=text, contract_text=contract_text)
                    entry[label] = [
                        {"rule": f.rule_id, "severity": f.severity, "actual": f.actual,
                         "expected": f.expected}
                        for f in rep.findings
                        if f.severity != "info"
                    ]  # fmt: skip
            report["scans"].append(entry)
            if args.dump is not None:
                args.dump.mkdir(parents=True, exist_ok=True)
                (args.dump / f"{pdf.stem}.ocr.txt").write_text(text, encoding="utf-8")
                (args.dump / f"{pdf.stem}.twin.txt").write_text(truth, encoding="utf-8")
            print(
                f"{pdf.name:>14} -> {twin.name[:48]:<48} cer {entry['cer']:.3f} "
                f"codes {entry['codes_read']} {entry['seconds']}s",
                flush=True,
            )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Worst-case inputs against the real code, with the time and peak memory each one costs.

Each scenario builds a hostile input of the kind that used to hurt (or that a limit now bounds) and
runs it through the code that handles it, in a FRESH process so its peak memory is its own. A
scenario passes when it finishes inside its time and memory ceilings; the ceilings are the targets
of the security work (docs/SECURITY.md has the before-and-after table).

    python scripts/audit_resources.py                # every scenario, a table, exit 1 on a miss
    python scripts/audit_resources.py --json out.json
    python scripts/audit_resources.py --scenario bbox_extreme     # one, as a child would run it

Synthetic inputs only; nothing here reads client files.
"""

# The scenario table reads best with one scenario per line.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import io
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


@dataclass(frozen=True)
class Scenario:
    name: str
    what: str
    seconds: float  # ceiling
    mb: float  # ceiling on peak resident memory of the whole process, imports included
    run: Callable[[], str]


class timed:  # noqa: N801 - used as a context manager, like ``open``
    """Time the operation under test, not the imports and the construction of its input."""

    seconds = 0.0

    def __enter__(self) -> timed:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        timed.seconds += time.perf_counter() - self._start


# --- scenarios -------------------------------------------------------------------------------------------------


def regex_reference_chain() -> str:
    from procurement_agent.extraction.rules import find_contract_numbers

    text = "ხელშეკრულება № " + "1/" * 600 + "x"
    with timed():
        found = find_contract_numbers(text)
    return f"{len(text)} chars, {len(found)} reference(s)"


def regex_reference_80() -> str:
    from procurement_agent.extraction.rules import find_contract_numbers

    text = "№ " + "1/" * 39 + "x"
    with timed():
        found = find_contract_numbers(text)
    return f"{len(text)} chars, {len(found)} reference(s)"


def bbox_extreme() -> str:
    from tests.conftest import make_pdf

    from procurement_agent.ingestion.render import render_page

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "a.pdf"
        pdf.write_bytes(make_pdf(300, 400))
        boxes = [(50.0, -7e8, 60.0, 7e8)] * 20 + [(float("nan"), 0.0, 1.0, 1.0)]
        with timed():
            png = render_page(pdf, 1, boxes)
    return f"{len(png)} byte page image with 21 extreme boxes"


def page_size_bomb() -> str:
    from tests.conftest import make_pdf

    from procurement_agent.ingestion.render import PageTooLargeError, render_page

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "a.pdf"
        pdf.write_bytes(make_pdf(10**7, 10**7))
        try:
            with timed():
                png = render_page(pdf, 1, [])
        except PageTooLargeError:
            return "refused: page too large to render"
    return f"rendered {len(png)} bytes"


def boq_cell_cap() -> str:
    from tests.boq_factory import LINES, build_boq, pad_workbook

    from procurement_agent.ingestion.xlsx_size import WorkbookTooLargeError, ensure_cell_count

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bid.xlsx"
        path.write_bytes(pad_workbook(build_boq(LINES), 400_000))
        try:
            with timed():
                ensure_cell_count(path, 300_000)
        except WorkbookTooLargeError:
            return f"{path.stat().st_size // 1024} KB workbook of 400,000 cells: refused before loading"
    return "accepted (unexpected)"


def zip_bomb_guard() -> str:
    from tests.conftest import make_ooxml

    from procurement_agent.security.upload_guard import UploadRejectedError, validate_upload

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("[Content_Types].xml", "word/document.xml"):
            with zipfile.ZipFile(io.BytesIO(make_ooxml("docx"))) as src:
                z.writestr(name, src.read(name))
        with z.open("word/media/bomb.bin", "w", force_zip64=True) as member:
            chunk = b"\0" * (1 << 20)
            for _ in range(400):
                member.write(chunk)
    try:
        with timed():
            validate_upload("a.docx", out.getvalue(), max_bytes=50 << 20, max_pages=300)
    except UploadRejectedError as e:
        return f"{len(out.getvalue()) // 1024} KB archive of 400 MiB: refused ({e})"
    return "accepted (unexpected)"


def upload_many_entries() -> str:
    from tests.conftest import make_ooxml

    from procurement_agent.security.upload_guard import validate_upload

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(make_ooxml("docx"))) as src, zipfile.ZipFile(out, "w") as z:
        for name in src.namelist():
            z.writestr(name, src.read(name))
        for i in range(9_900):
            z.writestr(f"word/p{i}.xml", b"<r/>")
    with timed():
        result = validate_upload("a.docx", out.getvalue(), max_bytes=50 << 20, max_pages=300)
    return f"{result.kind}, 9,900 entries validated"


def pdf_300_pages() -> str:
    from procurement_agent.security.upload_guard import validate_upload

    pages = 300
    bodies = [
        "<</Type/Catalog/Pages 2 0 R>>",
        f"<</Type/Pages/Kids[{' '.join(f'{3 + i} 0 R' for i in range(pages))}]/Count {pages}>>",
    ]
    bodies += ["<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>" for _ in range(pages)]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj{body}endobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(bodies) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    with timed():
        result = validate_upload("a.pdf", bytes(out), max_bytes=50 << 20, max_pages=300)
    return f"{result.page_count} pages validated"


def bound_text_20mb() -> str:
    from procurement_agent.ingestion.text_limits import bound_text

    text = " \n" * 10_000_000
    with timed():
        kept = len(bound_text(text))
    return f"{len(text) // 1_000_000} M characters of separators -> {kept}"


def bound_text_ordinary_20mb() -> str:
    from procurement_agent.ingestion.text_limits import bound_text

    text = "ხელშეკრულება № 17/2024, ღირებულება 120,000.00 ლარი. " * 400_000
    with timed():
        kept = len(bound_text(text))
    return f"{len(text) // 1_000_000} M characters of ordinary text, {kept} kept"


def long_paragraph_chunking() -> str:
    from docling_core.types.doc.document import DoclingDocument
    from docling_core.types.doc.labels import DocItemLabel

    from procurement_agent.ingestion.chunker import chunk_docling_document

    doc = DoclingDocument(name="synthetic")
    doc.add_text(label=DocItemLabel.TEXT, text="ხელშეკრულების მხარეები თანხმდებიან. " * 1500)
    with timed():
        chunks = chunk_docling_document(doc, doc_id="d" * 32)
    return f"one 54,000-character paragraph -> {len(chunks)} chunks"


def whitespace_docx_extraction() -> str:
    """A Word file whose body is 40,000 characters of mixed whitespace, through conversion, chunking
    and every extraction rule - the path where a quadratic pattern cost 0.6 s per chunk."""
    from docx import Document

    from procurement_agent.config import Settings
    from procurement_agent.extraction import rules
    from procurement_agent.extraction.pipeline import load_chunks, result_dir, upload_path
    from procurement_agent.ingestion import docling_service

    doc = Document()
    doc.add_paragraph("The price is 100" + " \n" * 20_000 + "tetri and more text. " + "word " * 500)
    buf = io.BytesIO()
    doc.save(buf)
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            env="dev", data_dir=Path(tmp) / "d", ollama_base_url="http://127.0.0.1:1",
            docling_artifacts_path=ROOT / "models" / "docling",
        )  # fmt: skip
        settings.ensure_dirs()
        doc_id = "a" * 32
        upload_path(settings, doc_id, "docx").write_bytes(buf.getvalue())
        docling_service.convert(upload_path(settings, doc_id, "docx"), settings)  # loads the models
        with timed():
            result = docling_service.convert(upload_path(settings, doc_id, "docx"), settings)
            docling_service.save_document(result.document, result_dir(settings, doc_id))
            chunks = load_chunks(settings, doc_id, "docx")
            facts = sum(len(rules.run_rules(c.body)) for c in chunks)
    return f"{len(chunks)} chunk(s), {facts} fact(s)"


def workbook_text_budget() -> str:
    from openpyxl import Workbook

    from procurement_agent.ingestion.excel_reader import read_workbook

    wb = Workbook()
    ws = wb.active or wb.create_sheet()
    row = ["x" * 5000] * 10
    for _ in range(400):
        ws.append(row)  # 20 million characters
    out = io.BytesIO()
    wb.save(out)
    notes: list[str] = []
    with timed():
        sheets = read_workbook(out.getvalue(), notes=notes)
    chars = sum(len(c.value) for s in sheets for c in s.cells)
    return f"20 M characters offered, {chars // 1000} K read, {len(notes)} note(s)"


SCENARIOS = [
    Scenario("regex_reference_chain", "a 1,200-character chain of slash-separated groups after a contract word (froze the server)", 0.5, 400, regex_reference_chain),
    Scenario("regex_reference_80", "the 80-character form of it (about 31 hours before the fix)", 0.2, 400, regex_reference_80),
    Scenario("bbox_extreme", "21 highlight boxes with +-7e8 and NaN coordinates (5.5 s of GIL each before)", 3.0, 700, bbox_extreme),
    Scenario("page_size_bomb", "a page declared 10^7 points square (a 2 GB bitmap before)", 3.0, 700, page_size_bomb),
    Scenario("boq_cell_cap", "a bid workbook of 400,000 cells (about 170 MB when loaded, twice)", 3.0, 500, boq_cell_cap),
    Scenario("zip_bomb_guard", "a 400 MiB part in a few hundred KB of zip", 3.0, 300, zip_bomb_guard),
    Scenario("upload_many_entries", "an archive of 9,900 entries (just under the limit)", 3.0, 300, upload_many_entries),
    Scenario("pdf_300_pages", "a 300-page PDF (the page limit) through the upload guard", 5.0, 400, pdf_300_pages),
    Scenario("bound_text_20mb", "20 million characters of whitespace through the text limit", 8.0, 800, bound_text_20mb),
    Scenario("bound_text_ordinary_20mb", "20 million characters of ordinary text through the text limit", 8.0, 800, bound_text_ordinary_20mb),
    Scenario("long_paragraph_chunking", "one 54,000-character paragraph (raised TypeError before)", 6.0, 1200, long_paragraph_chunking),
    Scenario("whitespace_docx_extraction", "40,000 characters of mixed whitespace through convert, chunk and every rule", 20.0, 2500, whitespace_docx_extraction),
    Scenario("workbook_text_budget", "a workbook of 20 million characters of cell text", 20.0, 2500, workbook_text_budget),
]  # fmt: skip


# --- running ---------------------------------------------------------------------------------------------------------


def child(name: str) -> int:
    scenario = next(s for s in SCENARIOS if s.name == name)
    detail = scenario.run()
    seconds = timed.seconds
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps({"seconds": seconds, "mb": peak_mb, "detail": detail}))
    return 0


def parent(only: str | None, json_out: Path | None) -> int:
    rows = []
    failed = 0
    for s in SCENARIOS:
        if only and not s.name.startswith(only):
            continue
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        started = time.perf_counter()
        try:
            done = subprocess.run(
                [sys.executable, __file__, "--scenario", s.name],
                capture_output=True, text=True, timeout=max(60.0, s.seconds * 5), env=env, check=False,
            )  # fmt: skip
            outcome = (
                json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else None
            )
        except subprocess.TimeoutExpired:
            outcome = None
        wall = time.perf_counter() - started
        if outcome is None:
            rows.append((s.name, "FAILED", None, None, "crashed or timed out", s))
            failed += 1
            continue
        ok = outcome["seconds"] <= s.seconds and outcome["mb"] <= s.mb
        failed += not ok
        rows.append(
            (
                s.name,
                "ok" if ok else "OVER",
                outcome["seconds"],
                outcome["mb"],
                outcome["detail"],
                s,
            )
        )
        _ = wall
    print(
        f"{'scenario':28s} {'result':6s} {'time':>8s} {'ceiling':>8s} {'peak MB':>8s} {'ceiling':>8s}  detail"
    )
    for name, result, seconds, mb, detail, s in rows:
        t = "-" if seconds is None else f"{seconds:7.2f}s"
        m = "-" if mb is None else f"{mb:7.0f}"
        print(f"{name:28s} {result:6s} {t:>8s} {s.seconds:7.1f}s {m:>8s} {s.mb:8.0f}  {detail}")
    print(f"\n{len(rows) - failed}/{len(rows)} scenarios inside their ceilings")
    if json_out:
        json_out.write_text(
            json.dumps(
                [
                    {"scenario": n, "result": r, "seconds": t, "mb": m, "detail": d}
                    for n, r, t, m, d, _ in rows
                ],
                indent=1,
            ),
            encoding="utf-8",
        )
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="run one scenario in this process (what the parent does)")
    ap.add_argument("--only", help="run the scenarios whose names start with this")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    if args.scenario:
        return child(args.scenario)
    return parent(args.only, args.json)


if __name__ == "__main__":
    sys.exit(main())

"""Measure conversion and extraction time per fixture on this machine (CPU).

uv run python scripts/benchmark.py            # rules only
uv run python scripts/benchmark.py --llm      # + LLM (needs Ollama; uses the fast model)
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from procurement_agent.config import Settings
from procurement_agent.extraction.pipeline import run_extraction
from procurement_agent.ingestion import docling_service
from procurement_agent.ingestion.chunker import chunk_docling_document
from procurement_agent.ingestion.excel_reader import read_workbook, sheet_chunks
from procurement_agent.llm.ollama_client import OllamaClient

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--files", nargs="*", default=None)
    args = ap.parse_args()

    settings = Settings(
        ollama_base_url=os.environ.get("PA_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        docling_artifacts_path=ROOT / "models" / "docling",
        llm_num_thread=min(16, os.cpu_count() or 4),
    )
    llm = OllamaClient(settings) if args.llm else None
    files = [Path(f) for f in args.files] if args.files else sorted(FIXTURES.glob("*.*"))

    print(f"{'file':32} {'parse s':>8} {'chunks':>6} {'extract s':>10} {'facts':>5} {'llm':>4}")
    for f in files:
        t0 = time.perf_counter()
        if f.suffix == ".xlsx":
            chunks = sheet_chunks(read_workbook(f.read_bytes()), doc_id=f.stem)
        else:
            doc = docling_service.convert(f, settings).document
            chunks = chunk_docling_document(doc, doc_id=f.stem)
        t_parse = time.perf_counter() - t0
        t0 = time.perf_counter()
        res = run_extraction(
            document_id=f.stem, chunks=chunks, settings=settings, llm=llm, fast=True
        )
        t_ext = time.perf_counter() - t0
        row = f"{f.name:32} {t_parse:8.1f} {len(chunks):6} {t_ext:10.1f}"
        print(f"{row} {len(res.facts):5} {res.llm_calls:4}")


if __name__ == "__main__":
    main()

"""No workbook this system writes may contain a live formula that a bidder typed.

The extraction, comparison and BOQ exports all carry text from documents other people wrote: file
names, sheet names, cell and paragraph text. A leading ``=``, ``+``, ``-`` or ``@`` in that text
becomes a formula in Excel, and the exports are the one kind of file the buyer trusts because this
system produced them. The documents here attack every place such text can arrive; every workbook
the API returns is then loaded and every cell of every sheet is checked.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.main import create_app
from tests.boq_factory import LINES, blank, build_boq

KEY = "pa_devkey_for_tests_only"
H = {"X-API-Key": KEY}
LEAD = ("=", "+", "-", "@", "\t", "\r")
#: What a hostile author puts at the start of text. Each is a working formula or DDE payload.
PAYLOADS = [
    '=HYPERLINK("http://evil.example/steal","click")',
    "+SUM(1,1)",
    "-2+3",
    "@SUM(1,1)",
    "=cmd|' /C calc'!A0",
    "\t=1+1",
    "\r=1+1",
]
SHEET_NAMES = ["=1+1", "+1+1", "-1+1", "@SUM(1,1)"]  # characters a sheet name may contain


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key=KEY,
        ollama_base_url="http://127.0.0.1:1",
        retention_days=1,
        rate_limit="100000/minute",
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


def _upload(client: TestClient, name: str, data: bytes) -> str:
    r = client.post("/api/v1/documents", headers=H, files={"file": (name, data)})
    assert r.status_code == 201, r.text
    deadline = time.time() + 60
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{r.json()['parse_job_id']}", headers=H).json()
        if job["status"] in ("done", "failed"):
            assert job["status"] == "done", job
            return str(r.json()["document"]["id"])
        time.sleep(0.05)
    raise AssertionError("parse did not finish")


def live_formulas(data: bytes) -> list[tuple[str, str, str]]:
    """Every cell that is a formula, or text a spreadsheet would read as one."""
    wb = load_workbook(io.BytesIO(data))
    found: list[tuple[str, str, str]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if cell.data_type == "f" or (isinstance(value, str) and value.startswith(LEAD)):
                    found.append((ws.title, cell.coordinate, str(value)[:60]))
    return found


def test_the_checker_itself_catches_a_formula() -> None:
    """The scan is only worth anything if it can fail."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "=1+1"
    ws["A2"] = "+1"
    ws["A3"] = "safe"
    out = io.BytesIO()
    wb.save(out)
    assert [c for _, c, _ in live_formulas(out.getvalue())] == ["A1", "A2"]


def _hostile_xlsx(sheet: str, amount: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet
    ws.append(["Item", "Description", "Quantity", "Unit", "Unit price (GEL)", "Total (GEL)"])
    for i, payload in enumerate(PAYLOADS, 1):
        ws.append([i, payload + " cable 3x2.5", 10, payload[:20] or "m", amount, 10 * amount])
    ws.append(["", "Warranty period: 24 months", "", "", "", ""])
    ws.append(["", "Total price", "", "", "Total:", f"{amount * 70} GEL"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _hostile_docx(amount: str) -> bytes:
    doc = Document()
    doc.add_paragraph("SUPPLY CONTRACT No. 17/2024")
    for i, payload in enumerate(PAYLOADS, 1):
        doc.add_paragraph(f"{payload} clause {i}: the price is {amount} GEL including VAT.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# --- extraction ---------------------------------------------------------------------------------


@pytest.mark.parametrize("sheet", SHEET_NAMES)
def test_the_extraction_workbook_has_no_live_formula(client: TestClient, sheet: str) -> None:
    doc = _upload(client, f"{sheet[0]}bid.xlsx", _hostile_xlsx(sheet, 100))
    job = client.post(f"/api/v1/documents/{doc}/extract", headers=H, json={}).json()
    deadline = time.time() + 60
    while time.time() < deadline:
        j = client.get(f"/api/v1/jobs/{job['id']}", headers=H).json()
        if j["status"] in ("done", "failed"):
            assert j["status"] == "done", j
            break
        time.sleep(0.05)
    r = client.get(f"/api/v1/jobs/{job['id']}/result", headers=H, params={"format": "xlsx"})
    assert r.status_code == 200
    assert live_formulas(r.content) == []
    # the attack was really delivered: the hostile sheet name is in the export, as text
    sheets_in_export = {
        str(c.value)
        for ws in load_workbook(io.BytesIO(r.content)).worksheets
        for row in ws.iter_rows()
        for c in row
    }
    assert f"'{sheet}" in sheets_in_export


def test_a_hostile_document_name_is_text_in_the_extraction_header(client: TestClient) -> None:
    doc = _upload(client, "=cmd.xlsx", _hostile_xlsx("Prices", 100))
    job = client.post(f"/api/v1/documents/{doc}/extract", headers=H, json={}).json()
    deadline = time.time() + 60
    while client.get(f"/api/v1/jobs/{job['id']}", headers=H).json()["status"] not in (
        "done",
        "failed",
    ):
        assert time.time() < deadline
        time.sleep(0.05)
    r = client.get(f"/api/v1/jobs/{job['id']}/result", headers=H, params={"format": "xlsx"})
    assert live_formulas(r.content) == []


# --- comparison ---------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["xlsx"])
def test_the_comparison_workbook_of_hostile_xlsx_files_has_no_live_formula(
    client: TestClient, fmt: str
) -> None:
    left = _upload(client, "=v1.xlsx", _hostile_xlsx("=1+1", 100))
    right = _upload(client, "+v2.xlsx", _hostile_xlsx("=1+1", 120))
    r = client.post(
        "/api/v1/checks/compare",
        headers=H,
        params={"format": fmt},
        json={"left_document_id": left, "right_document_id": right},
    )
    assert r.status_code == 200
    assert int(r.headers["X-Change-Count"]) >= 1  # the files really differ: the table has rows
    assert live_formulas(r.content) == []


def test_the_comparison_workbook_of_hostile_docx_files_has_no_live_formula(
    client: TestClient,
) -> None:
    left = _upload(client, "@v1.docx", _hostile_docx("120,000.00"))
    right = _upload(client, "-v2.docx", _hostile_docx("125,000.00"))
    r = client.post(
        "/api/v1/checks/compare",
        headers=H,
        params={"format": "xlsx"},
        json={"left_document_id": left, "right_document_id": right},
    )
    assert r.status_code == 200
    assert int(r.headers["X-Change-Count"]) >= 1
    assert live_formulas(r.content) == []


# --- bill of quantities --------------------------------------------------------------


def test_the_merged_boq_workbook_has_no_live_formula(client: TestClient) -> None:
    hostile = [
        (item, f"{PAYLOADS[i % len(PAYLOADS)]} {desc}", unit, q, m, lab)
        for i, (item, desc, unit, q, m, lab) in enumerate(LINES)
    ]
    template = _upload(
        client, "=template.xlsx", build_boq(blank(hostile), priced=False, stated_total=None)
    )
    bids = [
        _upload(client, "+bid-a.xlsx", build_boq(hostile)),
        _upload(client, "@bid-b.xlsx", build_boq(hostile, stated_total=2000.0)),
    ]
    r = client.post(
        "/api/v1/checks/boq",
        headers=H,
        params={"format": "xlsx"},
        json={"template_document_id": template, "bid_document_ids": bids},
    )
    assert r.status_code == 200, r.text[:200]
    assert live_formulas(r.content) == []
    # the attack was delivered: hostile names and descriptions are in the workbook, as text
    cells: list[Any] = [
        c.value
        for ws in load_workbook(io.BytesIO(r.content)).worksheets
        for row in ws.iter_rows()
        for c in row
    ]
    texts = {v for v in cells if isinstance(v, str)}
    assert "'+bid-a" in texts and "'@bid-b" in texts  # file names, written as text
    assert any(t.startswith("'+SUM(1,1) Steel") for t in texts)  # a description, as text

"""Regression tests for the defects the post-hardening review found.

Each test names the gap it closes: a fix that existed in one module but was never wired to the
call sites that matter, or a check that a looser rule had opened up.
"""

from __future__ import annotations

import inspect
import io
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook

from procurement_agent.api import checks as checks_api
from procurement_agent.checks.aoa import check_aoa
from procurement_agent.config import Settings
from procurement_agent.extraction import pipeline
from procurement_agent.extraction.anchors import find_parties
from procurement_agent.extraction.export import to_xlsx
from procurement_agent.extraction.schemas import ExtractionResult
from procurement_agent.ingestion import excel_reader
from procurement_agent.ingestion.chunker import MAX_CHUNK_TOKENS
from procurement_agent.jobs.worker import IDLE_STALL_S, Worker

FIXTURES = Path(__file__).parent.parent / "fixtures"

# Synthetic, checksum-valid accounts: one per party.
CUSTOMER_IBAN = "GE83TB0000000555000111"
CONTRACTOR_IBAN = "GE29NB0000000101904917"

CONTRACT = f"""ხელშეკრულება
საქართველო, თბილისი                    1 მარტი, 2026 წელი
შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი,
რუსთაველის 12 (შემდგომში „შემკვეთი“), ა/ა: {CUSTOMER_IBAN}, წარმოდგენილი დირექტორის ნინო
კაპანაძის სახით და
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით
1.1. „შემსრულებელი“ იღებს ვალდებულებას „მზე“ პროექტისთვის (ს/კ: 01.02.03.456) მომსახურების გაწევა.
2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.
4.1. ღირებულება შეადგენს 100 (ასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს.
4.3. ანგარიშსწორება: სს „საქართველოს ბანკი“, ბანკის კოდი: AAAAGE22,
ანგარიშის ნომერი: {CONTRACTOR_IBAN}.
"""

ACT = f"""მიღება-ჩაბარების აქტი
შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი,
რუსთაველის 12 (შემდგომში „შემკვეთი“) წარმოდგენილი დირექტორის ნინო კაპანაძის სახით და
შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321, იურიდიული მისამართი: ქ. ბათუმი,
ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)
„შემკვეთის“ პროექტ „მზე“ (ს/კ: 01.02.03.456) მომსახურების გაწევა.
2. შესრულების თარიღი
მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.
3. საფასური
საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით. ბანკი: სს „საქართველოს ბანკი“;
ბანკის კოდი: AAAAGE22; ა/ა: {CONTRACTOR_IBAN}
4. გაფორმების თარიღი
აქტი გაფორმდა 2026 წლის 30 აპრილი
„შემკვეთი“
შპს „ალფა ჯგუფი“
დირექტორი ნინო კაპანაძე
„შემსრულებელი“
შპს „ბეტა სერვისი“
დირექტორი: დავით მელაძე
"""


# --- AOA: the act must name the contractor's account, not the customer's -------------------


def test_each_account_is_attributed_to_the_party_whose_block_states_it() -> None:
    by_role = {p.role: p.accounts for p in find_parties(CONTRACT)}
    assert by_role["შემკვეთი"] == [CUSTOMER_IBAN]
    # the contractor's account follows a bank's name, which is not a party of its own
    assert by_role["შემსრულებელი"] == [CONTRACTOR_IBAN]


def test_an_act_with_the_contractors_account_is_clean() -> None:
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
    assert [f.rule_id for f in report.errors] == []


def test_an_act_with_the_customers_account_is_an_error() -> None:
    """Both accounts are in the contract, so the subset test alone let this through."""
    act = ACT.replace(CONTRACTOR_IBAN, CUSTOMER_IBAN)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT)
    bank = [f for f in report.errors if f.rule_id == "AOA.BANK"]
    assert len(bank) == 1
    assert bank[0].actual == CUSTOMER_IBAN
    assert bank[0].expected == CONTRACTOR_IBAN


def test_no_owner_claim_when_the_roles_were_not_read() -> None:
    no_roles = CONTRACT.replace("(შემდგომში „შემკვეთი“)", "").replace(
        "(შემდგომში „შემსრულებელი“)", ""
    )
    act = ACT.replace(CONTRACTOR_IBAN, CUSTOMER_IBAN)
    report = check_aoa(aoa_text=act, contract_text=no_roles)
    assert not any("belongs to the customer" in f.message_en for f in report.findings)


def test_a_side_by_side_requisites_table_makes_no_owner_claim() -> None:
    """Both accounts in one party's block: which is whose cannot be read, so say nothing."""
    table = (
        CONTRACT.replace(f", ა/ა: {CUSTOMER_IBAN},", ",")
        + f"რეკვიზიტები: {CONTRACTOR_IBAN} | {CUSTOMER_IBAN}\n"
    )
    for act in (ACT, ACT.replace(CONTRACTOR_IBAN, CUSTOMER_IBAN)):
        report = check_aoa(aoa_text=act, contract_text=table)
        assert not any("belongs to the customer" in f.message_en for f in report.findings)


def test_the_customer_named_last_with_the_contractors_account_after_it_is_clean() -> None:
    """The last party's block runs to the end, so the contractor's account stated after the
    customer's signature line lands in the customer's block as well as the contractor's."""
    tail = f"„შემკვეთი“\nშპს „ალფა ჯგუფი“\nანგარიში: {CONTRACTOR_IBAN}\n"
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT + tail)
    assert [f.rule_id for f in report.errors] == []


# --- every workbook writer escapes untrusted text --------------------------------------------


def test_the_extraction_export_survives_control_characters_and_escapes_formulas() -> None:
    result = ExtractionResult(
        document_id="d", facts=[], targets=[], warnings=['=HYPERLINK("x")', "tab\x0bbed"]
    )
    wb = load_workbook(io.BytesIO(to_xlsx(result, "=cmd|' /C calc'!A0")))
    cells = [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value]
    assert not any(isinstance(v, str) and v.startswith("=") for v in cells)
    assert "tabbed" in cells


def test_the_boq_compare_script_escapes_bidder_text() -> None:
    sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
    try:
        import boq_compare
    finally:
        sys.path.pop(0)
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    boq_compare._append(ws, ["=SUM(A1:A9)", "@x", 5.0, None])
    assert [c.value for c in ws[1]] == ["'=SUM(A1:A9)", "'@x", 5.0, None]


# --- comparison: OCR leniency and truncation reach production callers ------------------------


def _settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    return s


def test_only_a_pdf_without_text_is_treated_as_a_scan(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    shutil.copy(FIXTURES / "offer_ka_en.pdf", pipeline.upload_path(s, "digital", "pdf"))
    shutil.copy(FIXTURES / "offer_ka_en_scanned.pdf", pipeline.upload_path(s, "scan", "pdf"))
    assert pipeline.is_scanned_pdf(s, "digital", "pdf") is False
    assert pipeline.is_scanned_pdf(s, "scan", "pdf") is True
    assert pipeline.is_scanned_pdf(s, "anything", "docx") is False


def test_compare_stored_tells_compare_whether_the_input_was_ocrd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_compare(left: object, right: object, **kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(skipped=[])

    monkeypatch.setattr(pipeline, "compare", fake_compare)
    monkeypatch.setattr(pipeline, "load_segments", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "is_scanned_pdf", lambda s, doc_id, kind: doc_id == "scan")
    s = _settings(tmp_path)
    a = pipeline.StoredDocument(id="a", kind="pdf", name="a")
    pipeline.compare_stored(s, a, pipeline.StoredDocument(id="b", kind="pdf", name="b"))
    assert seen["ocr"] is False
    pipeline.compare_stored(s, a, pipeline.StoredDocument(id="scan", kind="pdf", name="s"))
    assert seen["ocr"] is True


def _workbook(rows: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for r in range(1, rows + 1):
        ws.append([f"item {r}", r * 1.5])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_truncated_workbook_is_reported_as_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(excel_reader, "_MAX_ROWS", 5)
    s = _settings(tmp_path)
    for doc_id in ("l", "r"):
        pipeline.upload_path(s, doc_id, "xlsx").write_bytes(_workbook(12))
    report = pipeline.compare_stored(
        s,
        pipeline.StoredDocument(id="l", kind="xlsx", name="l"),
        pipeline.StoredDocument(id="r", kind="xlsx", name="r"),
    )
    notes = [n for n in report.skipped if n.rule_id == "CMP.TRUNCATED"]
    assert notes, "two workbooks compared on their first rows must not read as identical"
    assert "5 rows" in notes[0].reason_en


def test_extraction_learns_what_the_workbook_reader_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(excel_reader, "_MAX_ROWS", 5)
    s = _settings(tmp_path)
    pipeline.upload_path(s, "x", "xlsx").write_bytes(_workbook(12))
    notes: list[str] = []
    pipeline.load_chunks(s, "x", "xlsx", notes=notes)
    assert notes
    assert "5 rows" in notes[0]


# --- BOQ merged workbook from the API carries the rate outliers ------------------------------


def test_the_api_merged_workbook_includes_rate_outliers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_write(template: object, books: object, comps: object, out: Path, **kw: Any) -> Path:
        captured.update(kw)
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(checks_api, "write_merged_workbook", fake_write)
    outliers = [object()]
    report = SimpleNamespace(bidders=[], rate_outliers=outliers, template_findings=[])
    checks_api.merged_xlsx(
        object(),  # type: ignore[arg-type]
        [],
        [],
        SimpleNamespace(id="0123456789abcdef"),  # type: ignore[arg-type]
        report,  # type: ignore[arg-type]
    )
    assert captured["outliers"] is outliers


# --- chunk size and token budget follow the corrected token estimate -------------------------


def test_sheet_chunks_default_to_the_shared_chunk_size() -> None:
    default = inspect.signature(excel_reader.sheet_chunks).parameters["max_tokens"].default
    assert default == MAX_CHUNK_TOKENS


def test_the_token_budget_default_matches_the_corrected_estimate() -> None:
    # 6000 under the old estimate selected the text that ~7800 selects under the new one
    assert Settings.model_fields["llm_chunk_token_budget"].default == 7800


# --- /ready does not call a legitimately slow job stalled -------------------------------------


def test_a_busy_worker_is_allowed_several_llm_calls_between_beats(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path, llm_timeout_s=100, docling_timeout_s=50)
    w = Worker(s, None)
    w._thread = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
    w._busy_since = 0.0
    # an answer, a retry after invalid JSON and a verifier question: three full timeouts
    w._heartbeat -= 2.5 * 100 + IDLE_STALL_S
    assert w.healthy()
    w._heartbeat -= 100
    assert not w.healthy()


# --- numbers: a thousands group is exactly three digits --------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("12.3450", 12.345),
        ("1234.5670", 1234.567),
        ("1,234.5678", 1234.5678),
        # unchanged readings
        ("1.250,50", 1250.5),
        ("48 750,00", 48750.0),
        ("12,500", 12500.0),
        ("0.125", 0.125),
    ],
)
def test_a_separator_before_four_digits_is_a_decimal_point(text: str, value: float) -> None:
    from procurement_agent.extraction.normalize import parse_number

    assert parse_number(text) == pytest.approx(value)


@pytest.mark.parametrize("value", [12.345, 1234.567, -0.125, 12.35, 7.0])
def test_spreadsheet_numbers_read_back_as_themselves(value: float) -> None:
    """Chunk text feeds the rules and the model; 12.345 must not come back as 12345."""
    from procurement_agent.extraction.normalize import parse_number

    assert parse_number(excel_reader._fmt(value)) == pytest.approx(value)

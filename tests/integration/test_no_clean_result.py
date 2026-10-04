"""The rule of this change, in one table: a document, or part of one, that was not read never
gives a result that looks like "all clear".

Every report (the act check, the comparison, the extraction, the BOQ) is run on every kind of
degraded input that can reach it. Each run must end in a refusal that names why, a result that
says in so many words that part of the input was not read, or a job that failed - and never in a
bare result with nothing to show it was built on less than the whole.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from docling.datamodel.base_models import ConversionStatus
from fastapi.testclient import TestClient

from tests.boq_factory import LINES, blank, build_boq
from tests.degraded_support import (
    ACT_LINES,
    CONTRACT_LINES,
    XLSX,
    H,
    Plan,
    damage_parse,
    document,
    install_fake_convert,
    make_client,
    make_legacy,
    multipage_pdf,
    upload,
    wait_job,
    xlsx_bytes,
)

REFUSAL_CODES = {"NO_TEXT", "DOCUMENT_UNREADABLE"}

#: (route, degraded input) for every pair that can happen. A BOQ has no pages to lose, so it has
#: no 'partial'; a comparison's two sides are either one's document, so only one is degraded.
CASES = [
    ("aoa", "empty_text"),
    ("aoa", "partial"),
    ("aoa", "missing_parse"),
    ("aoa", "garbled_parse"),
    ("aoa", "unreadable_contract"),
    ("aoa", "partial_contract"),
    ("compare", "empty_text"),
    ("compare", "partial"),
    ("compare", "missing_parse"),
    ("compare", "garbled_parse"),
    ("extract", "empty_text"),
    ("extract", "partial"),
    ("extract", "missing_parse"),
    ("extract", "garbled_parse"),
    ("boq", "empty_text"),
    ("boq", "unrecognised_template"),
    ("boq", "garbled_parse"),
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


@pytest.fixture
def register(monkeypatch: pytest.MonkeyPatch) -> Callable[[bytes, Plan], bytes]:
    return install_fake_convert(monkeypatch, ocr=False)


class _Docs:
    def __init__(self, client: TestClient, register: Callable[[bytes, Plan], bytes]) -> None:
        self.client, self.register = client, register
        self.n = 0

    def good(self, lines: list[str] | None = None) -> str:
        self.n += 1
        return upload(self.client, f"good{self.n}.xlsx", xlsx_bytes(lines or ACT_LINES), XLSX)[0]

    def pdf(self, plan: Plan) -> str:
        self.n += 1
        data = self.register(multipage_pdf(plan.pages, f"d{self.n}"), plan)
        return upload(self.client, f"d{self.n}.pdf", data)[0]

    def degraded(self, how: str) -> str:
        """One document, degraded the named way."""
        text = {1: "alpha text here", 2: "beta text here"}
        if how == "empty_text":
            doc = self.good()
            make_legacy(self.client, doc, "xlsx")
            return doc
        if how in ("partial", "partial_contract"):
            return self.pdf(
                Plan(pages=5, text_on=text, status=ConversionStatus.PARTIAL_SUCCESS, errors=2)
            )
        doc = self.pdf(Plan(pages=2, text_on=text))
        damage_parse(self.client, doc, delete=how == "missing_parse")
        return doc


def _classify(r: Any, *, partial_in: str | None = None) -> str:
    """What came back: refused, noted, or (the failure this test exists for) clean."""
    if r.status_code == 409 and isinstance(r.json().get("detail"), dict):
        return "refused" if r.json()["detail"]["code"] in REFUSAL_CODES else "clean"
    if r.status_code != 200:
        return f"unexpected {r.status_code}"
    body = r.json()
    notes = [s["rule_id"] for s in body.get("skipped", [])]
    quality = body.get("read_quality", {})
    if "DOC.PARTIAL" in notes and any(q["partial"] for q in quality.values()):
        return "noted"
    findings = [f for f in body.get("template_findings", []) if f["severity"] == "error"]
    if findings and findings[0]["rule_id"] == "BOQ.TEMPLATE_UNRECOGNISED":
        return "noted"
    return "clean"


def _aoa(d: _Docs, how: str) -> str:
    degraded = d.degraded(how if how != "unreadable_contract" else "empty_text")
    other = d.good(CONTRACT_LINES if how != "partial_contract" else ACT_LINES)
    ids = (
        {"aoa_document_id": other, "contract_document_id": degraded}
        if how in ("unreadable_contract", "partial_contract")
        else {"aoa_document_id": degraded, "contract_document_id": other}
    )
    return _classify(d.client.post("/api/v1/checks/aoa", headers=H, json=ids))


def _compare(d: _Docs, how: str) -> str:
    left, right = d.good(), d.degraded(how)
    r = d.client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": left, "right_document_id": right},
    )
    return _classify(r)


def _extract(d: _Docs, how: str) -> str:
    doc = d.degraded(how)
    r = d.client.post(f"/api/v1/documents/{doc}/extract", headers=H, json={})
    if r.status_code == 409:
        return "refused" if r.json()["detail"]["code"] in REFUSAL_CODES else "clean"
    assert r.status_code == 202, r.text
    job = wait_job(d.client, r.json()["id"])
    if job["status"] == "failed":
        return "refused"
    result = d.client.get(f"/api/v1/jobs/{job['id']}/result", headers=H).json()
    flagged = result["read_quality"]["document"]["partial"] and any(
        w.startswith("DOC.PARTIAL") for w in result["warnings"]
    )
    return "noted" if flagged else "clean"


def _boq(d: _Docs, how: str) -> str:
    bid = upload(d.client, "bid.xlsx", build_boq(LINES, stated_total=1000.0), XLSX)[0]
    if how == "unrecognised_template":
        template = upload(d.client, "t.xlsx", xlsx_bytes(["some words", "and more"]), XLSX)[0]
    else:
        template = upload(
            d.client, "t.xlsx", build_boq(blank(), priced=False, stated_total=None), XLSX
        )[0]
        if how == "empty_text":
            make_legacy(d.client, template, "xlsx")
        else:  # garbled: the stored workbook is no workbook any more
            from tests.degraded_support import settings_of

            (settings_of(d.client).upload_dir / f"{template}.xlsx").write_bytes(b"not a workbook")
            (settings_of(d.client).results_dir / template / "read_quality.json").unlink(
                missing_ok=True
            )
    r = d.client.post(
        "/api/v1/checks/boq",
        headers=H,
        json={"template_document_id": template, "bid_document_ids": [bid]},
    )
    return _classify(r)


RUNNERS = {"aoa": _aoa, "compare": _compare, "extract": _extract, "boq": _boq}


@pytest.mark.parametrize(("route", "how"), CASES, ids=[f"{r}-{h}" for r, h in CASES])
def test_a_degraded_input_never_gives_a_clean_result(
    client: TestClient, register: Callable[[bytes, Plan], bytes], route: str, how: str
) -> None:
    outcome = RUNNERS[route](_Docs(client, register), how)
    assert outcome in ("refused", "noted"), f"{route} on {how}: {outcome}"


def test_the_control_a_fully_read_input_is_clean_and_says_nothing_extra(
    client: TestClient, register: Callable[[bytes, Plan], bytes]
) -> None:
    """The table above would pass if everything were refused; this is the case that is not."""
    d = _Docs(client, register)
    act, contract = d.good(), d.good(CONTRACT_LINES)
    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    )
    assert r.status_code == 200
    assert _classify(r) == "clean"
    assert document(client, act)["read_quality"]["partial"] is False

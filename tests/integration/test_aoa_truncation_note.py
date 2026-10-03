"""What the workbook reader left out (rows, columns or cells past its caps) is in the act report,
as it already is in the comparison and the extraction."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from procurement_agent.ingestion import excel_reader
from tests.degraded_support import (
    ACT_LINES,
    CONTRACT_LINES,
    XLSX,
    H,
    make_client,
    skip_ids,
    upload,
    xlsx_bytes,
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


def test_the_act_report_says_what_the_workbook_reader_left_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(excel_reader, "_MAX_ROWS", 3)
    act, _ = upload(client, "act.xlsx", xlsx_bytes([*ACT_LINES, "row five", "row six"]), XLSX)
    contract, _ = upload(client, "contract.xlsx", xlsx_bytes(CONTRACT_LINES), XLSX)
    body = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": act, "contract_document_id": contract},
    ).json()
    assert skip_ids(body).count("AOA.TRUNCATED") >= 1
    note = next(s for s in body["skipped"] if s["rule_id"] == "AOA.TRUNCATED")
    assert "size limit" in note["reason_en"]
    assert note["reason_ka"] != note["reason_en"]


def test_a_workbook_inside_the_limits_adds_no_truncation_note(client: TestClient) -> None:
    act, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    body = client.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": act}).json()
    assert "AOA.TRUNCATED" not in skip_ids(body)

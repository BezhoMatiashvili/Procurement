"""Times say their zone: the API gives every timestamp an offset, the pages label theirs."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.degraded_support import ACT_LINES, XLSX, H, make_client, upload, xlsx_bytes


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


def test_the_api_gives_every_timestamp_an_explicit_utc_offset(client: TestClient) -> None:
    doc_id, job_id = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    doc = client.get(f"/api/v1/documents/{doc_id}", headers=H).json()
    job = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
    listed = client.get("/api/v1/documents", headers=H).json()[0]
    stamps = [
        doc["created_at"],
        doc["expires_at"],
        listed["created_at"],
        job["created_at"],
        job["started_at"],
        job["finished_at"],
    ]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", s) for s in stamps), stamps


def test_the_pages_label_their_times_utc(client: TestClient) -> None:
    doc_id, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    documents = client.get("/ui/documents").text
    one = client.get(f"/ui/documents/{doc_id}").text
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", documents)
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", one)  # expiry

"""The scanned-PDF banner claims OCR only when OCR ran."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.degraded_support import (
    SAME_ORIGIN,
    Plan,
    install_fake_convert,
    make_client,
    multipage_pdf,
    upload,
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


def _english(client: TestClient) -> None:
    client.post("/ui/lang", data={"lang": "en", "next": "/ui/"}, headers=SAME_ORIGIN)


@pytest.mark.parametrize(
    ("ocr", "claims_ocr", "says_unavailable"),
    [(True, True, False), (False, False, True)],
    ids=["ocr-ran", "ocr-off"],
)
def test_the_reading_page_claims_ocr_only_when_ocr_ran(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    ocr: bool,
    claims_ocr: bool,
    says_unavailable: bool,
) -> None:
    register: Callable[[bytes, Plan], bytes] = install_fake_convert(monkeypatch, ocr=ocr)
    data = register(multipage_pdf(1, f"o{ocr}"), Plan(pages=1, text_on={1: "words on page"}))
    _, job_id = upload(client, "s.pdf", data)
    _english(client)
    page = client.get(f"/ui/jobs/{job_id}").text
    assert ("recognised from the page images" in page) is claims_ocr
    assert ("OCR was not available" in page) is says_unavailable

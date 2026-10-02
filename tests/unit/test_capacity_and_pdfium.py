"""Bounded work and the one-at-a-time rule for pdfium."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import HTTPException

from procurement_agent.ingestion import render, segmenter
from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK
from procurement_agent.security import upload_guard
from procurement_agent.security.capacity import Capacity, report_capacity
from tests.conftest import make_pdf

# --- Capacity -----------------------------------------------------------------------------------


def test_a_slot_is_given_while_there_is_one() -> None:
    cap = Capacity(2, wait_s=0.01)
    with cap.slot(), cap.slot():
        pass


def test_a_full_house_answers_503_with_retry_after_instead_of_waiting_forever() -> None:
    cap = Capacity(2, wait_s=0.05)
    with cap.slot(), cap.slot():
        started = time.monotonic()
        with pytest.raises(HTTPException) as refused, cap.slot():
            pass
        assert time.monotonic() - started < 1.0
    assert refused.value.status_code == 503
    assert refused.value.headers == {"Retry-After": "5"}
    assert "busy" in str(refused.value.detail)


def test_a_slot_is_released_when_the_work_fails() -> None:
    cap = Capacity(1, wait_s=0.01)
    with pytest.raises(RuntimeError), cap.slot():
        raise RuntimeError("the report crashed")
    with cap.slot():  # not leaked
        pass


def test_a_slot_is_released_for_the_next_waiter() -> None:
    cap = Capacity(1, wait_s=2.0)
    order: list[str] = []
    gate = threading.Event()

    def first() -> None:
        with cap.slot():
            order.append("first in")
            gate.set()
            time.sleep(0.2)
            order.append("first out")

    def second() -> None:
        gate.wait()
        with cap.slot():  # waits for the first, within wait_s
            order.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert order == ["first in", "first out", "second in"]


def test_many_threads_never_exceed_the_slots() -> None:
    cap = Capacity(3, wait_s=5.0)
    running = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal running, peak
        with cap.slot():
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.02)
            with lock:
                running -= 1

    threads = [threading.Thread(target=work) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak == 3


def test_one_capacity_per_size_in_the_process() -> None:
    assert report_capacity(4, 2.0) is report_capacity(4, 2.0)
    assert report_capacity(4, 2.0) is not report_capacity(5, 2.0)


# --- the pdfium lock ------------------------------------------------------------------------------


class _SpyDocument:
    """Stands in for ``pdfium.PdfDocument``; records whether the lock was held on every call."""

    held: ClassVar[list[bool]] = []
    inside = 0
    peak = 0
    guard = threading.Lock()

    def __init__(self, *_: object, **__: object) -> None:
        self._enter()

    @classmethod
    def _enter(cls) -> None:
        cls.held.append(PDFIUM_LOCK._is_owned())  # type: ignore[attr-defined]
        with cls.guard:
            cls.inside += 1
            cls.peak = max(cls.peak, cls.inside)
        time.sleep(0.002)

    def __len__(self) -> int:
        self.held.append(PDFIUM_LOCK._is_owned())  # type: ignore[attr-defined]
        return 1

    def __getitem__(self, _: int) -> _SpyPage:
        return _SpyPage()

    def close(self) -> None:
        self.held.append(PDFIUM_LOCK._is_owned())  # type: ignore[attr-defined]
        with self.guard:
            type(self).inside -= 1


class _SpyPage:
    def get_size(self) -> tuple[float, float]:
        return (200.0, 200.0)

    def render(self, scale: float = 1.0) -> _SpyBitmap:
        _SpyDocument.held.append(PDFIUM_LOCK._is_owned())  # type: ignore[attr-defined]
        return _SpyBitmap()

    def get_textpage(self) -> _SpyTextPage:
        return _SpyTextPage()

    def close(self) -> None:
        pass


class _SpyBitmap:
    def to_pil(self) -> object:
        from PIL import Image

        return Image.new("RGB", (4, 4), "white")


class _SpyTextPage:
    def get_text_range(self) -> str:
        return ""

    def close(self) -> None:
        pass


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> type[_SpyDocument]:
    _SpyDocument.held = []
    _SpyDocument.inside = 0
    _SpyDocument.peak = 0
    monkeypatch.setattr(render.pdfium, "PdfDocument", _SpyDocument)
    monkeypatch.setattr(segmenter.pypdfium2, "PdfDocument", _SpyDocument)
    import pypdfium2

    monkeypatch.setattr(pypdfium2, "PdfDocument", _SpyDocument)
    return _SpyDocument


def test_every_pdfium_call_of_the_application_holds_the_lock(
    spy: type[_SpyDocument], tmp_path: Path
) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf(200, 200))
    render.render_page(pdf, 1, [])
    segmenter.pdf_has_text_layer(pdf)
    upload_guard._check_pdf(b"%PDF-1.4 " + b"x" * 100, max_pages=10)
    assert len(spy.held) >= 6
    assert all(spy.held), spy.held


def test_two_threads_are_never_inside_pdfium_at_once(
    spy: type[_SpyDocument], tmp_path: Path
) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf(200, 200))

    def hammer() -> None:
        for _ in range(25):
            render.render_page(pdf, 1, [])
            segmenter.pdf_has_text_layer(pdf)
            upload_guard._check_pdf(b"%PDF-1.4 " + b"x" * 100, max_pages=10)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert spy.peak == 1, f"{spy.peak} threads were inside pdfium together"


def test_without_the_lock_the_spy_would_have_seen_the_overlap(
    spy: type[_SpyDocument], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check above is only as good as the spy: remove the lock and the overlap shows."""
    import contextlib

    monkeypatch.setattr(render, "PDFIUM_LOCK", contextlib.nullcontext())
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf(200, 200))

    def hammer() -> None:
        for _ in range(25):
            render.render_page(pdf, 1, [])

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert spy.peak > 1

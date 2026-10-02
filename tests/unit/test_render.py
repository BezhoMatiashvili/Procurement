"""Page rendering: what the caller or the PDF asks for must not decide what the server spends.

Pillow draws a rectangle by walking its extent with the GIL held, so a box with an edge at 7e8
stopped the whole process for five seconds - and the API took up to twenty boxes. The PDF states
its own page size, so a one-page file could ask for a multi-gigabyte bitmap.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from procurement_agent.ingestion.render import (
    MAX_PIXELS,
    PageTooLargeError,
    _fit_scale,
    _pixel_rect,
    render_page,
)
from tests.conftest import make_pdf


def _pdf(tmp_path: Path, width: int = 200, height: int = 200) -> Path:
    path = tmp_path / "page.pdf"
    path.write_bytes(make_pdf(width, height))
    return path


def test_boxes_with_an_extreme_edge_are_clamped_and_cheap(tmp_path: Path) -> None:
    started = time.perf_counter()
    png = render_page(_pdf(tmp_path), 1, [(50.0, -7e8, 60.0, 7e8)] * 20)
    assert time.perf_counter() - started < 3.0  # five seconds per such box before
    assert Image.open(io.BytesIO(png)).size == (300, 300)  # 200 points at the 1.5 scale


@pytest.mark.parametrize(
    "box",
    [
        (float("nan"), 0.0, 10.0, 10.0),
        (0.0, float("inf"), 10.0, 10.0),
        (0.0, 0.0, float("-inf"), 10.0),
    ],
)
def test_a_box_that_is_not_finite_is_not_drawn(box: tuple[float, float, float, float]) -> None:
    assert _pixel_rect(box, 1.5, (300, 300)) is None


@pytest.mark.parametrize(
    "box", [(400.0, 10.0, 500.0, 20.0), (-50.0, 10.0, -10.0, 20.0), (10.0, 400.0, 20.0, 500.0)]
)
def test_a_box_outside_the_page_is_not_drawn(box: tuple[float, float, float, float]) -> None:
    assert _pixel_rect(box, 1.0, (300, 300)) is None


def test_a_box_is_pulled_inside_the_page_with_its_edges_in_order() -> None:
    assert _pixel_rect((100.0, 150.0, -1e9, -1e9), 1.0, (300, 300)) == (0.0, 0.0, 100.0, 150.0)
    assert _pixel_rect((10.0, 20.0, 1e12, 1e12), 2.0, (300, 400)) == (20.0, 40.0, 300.0, 400.0)


def test_only_the_first_boxes_are_drawn(tmp_path: Path) -> None:
    one = render_page(_pdf(tmp_path), 1, [(10.0, 10.0, 50.0, 50.0)])
    many = render_page(_pdf(tmp_path), 1, [(10.0, 10.0, 50.0, 50.0)] * 500)
    assert one == many  # the same box twenty times is the same picture as once


def test_a_page_the_pdf_makes_huge_is_rendered_inside_the_pixel_cap(tmp_path: Path) -> None:
    png = render_page(_pdf(tmp_path, 14400, 14400), 1, [])
    width, height = Image.open(io.BytesIO(png)).size
    assert width * height <= MAX_PIXELS * 1.01  # the bitmap size rounds up by a pixel


def test_the_wanted_scale_is_kept_when_the_page_fits() -> None:
    assert _fit_scale(612.0, 792.0, 1.5) == 1.5


def test_the_scale_shrinks_to_the_cap() -> None:
    scale = _fit_scale(14400.0, 14400.0, 1.5)
    assert 0.1 < scale < 1.5
    assert 14400 * 14400 * scale * scale <= MAX_PIXELS * 1.0001


@pytest.mark.parametrize(
    ("width", "height"),
    [(0.0, 10.0), (10.0, -1.0), (float("nan"), 10.0), (float("inf"), 10.0), (200_000.0, 200_000.0)],
)
def test_a_page_with_no_usable_size_is_refused(width: float, height: float) -> None:
    with pytest.raises(PageTooLargeError):
        _fit_scale(width, height, 1.5)


def test_a_page_out_of_range_is_still_a_plain_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="out of range") as raised:
        render_page(_pdf(tmp_path), 2, [])
    assert not isinstance(raised.value, PageTooLargeError)

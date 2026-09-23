"""Regression tests for the prototype scripts under ``scripts/``. Synthetic data only."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from openpyxl import Workbook

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


aoa_check = _load("aoa_check")
boq_compare = _load("boq_compare")


# --- aoa_check -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2026, 8, 31), 1, date(2026, 9, 30)),
        (date(2026, 3, 15), 12, date(2027, 3, 15)),
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
    ],
)
def test_adding_months_clamps_to_the_last_day(start: date, months: int, expected: date) -> None:
    """date(..., start.day) crashed for a term that starts on the 31st."""
    assert aoa_check.add_months(start, months) == expected


def test_a_contract_starting_on_the_31st_does_not_crash_the_check() -> None:
    contract = {
        "facts": [
            {"type": "contract_date", "normalized": "2026-01-31", "value": "", "conflict": False},
            {"type": "duration", "normalized": "P1M", "value": "", "conflict": False},
        ]
    }
    aoa = {"facts": []}
    assert isinstance(aoa_check.check(contract, aoa), list)


def test_an_http_error_is_reported_not_a_key_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(httpx.HTTPStatusError):
        aoa_check.load_result("job-1", base="http://127.0.0.1:8001", key="k", transport=transport)


def test_a_job_result_is_fetched_with_the_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"facts": []})

    got = aoa_check.load_result(
        "job-1", base="http://127.0.0.1:8001", key="k", transport=httpx.MockTransport(handler)
    )
    assert got == {"facts": []}
    assert seen[0].headers["X-API-Key"] == "k"
    assert seen[0].url.path == "/api/v1/jobs/job-1/result"


# --- boq_compare ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,5", 1.5),
        ("1 234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("12,500", 12500.0),
        (7, 7.0),
        (2.25, 2.25),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_quantities_are_parsed_with_the_shared_number_parser(
    raw: object, expected: float | None
) -> None:
    """'1,5' used to lose its decimal comma and read as 15."""
    assert boq_compare._num(raw) == expected


def test_a_sheet_without_a_unit_column_is_read(tmp_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "1. EARTHWORKS"
    ws.append(["No", "Sub", "Description", "Quantity"])
    ws.append([1, None, "Excavation", "1,5"])
    path = tmp_path / "boq.xlsx"
    wb.save(path)
    lines = boq_compare.read_lines(path)
    (line,) = [v for k, v in lines.items() if k[2] == "Excavation"]
    assert line.qty == 1.5
    assert line.unit is None

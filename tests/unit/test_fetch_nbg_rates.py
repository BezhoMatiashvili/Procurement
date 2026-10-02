"""The NBG rates downloader (scripts/fetch_nbg_rates.py), run against canned answers - no network.

The answers have the shape the National Bank's API returned on 2026-10-02: a list holding one day
with its currencies, each with a quantity, a formatted rate and the day it is in force from. The
two USD rates are the client's example; the rest is invented.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from procurement_agent.fx.rates_table import parse_rates

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch_nbg_rates.py"
_spec = importlib.util.spec_from_file_location("fetch_nbg_rates", _SCRIPT)
assert _spec is not None and _spec.loader is not None
fetcher: ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetcher)


def _answer(valid_from: str, *entries: tuple[str, int, str]) -> bytes:
    return json.dumps(
        [
            {
                "date": f"{valid_from}T00:00:00.000Z",
                "currencies": [
                    {
                        "code": code,
                        "quantity": quantity,
                        "rateFormated": rate,
                        "rate": float(rate) if rate.replace(".", "").isdigit() else 0,
                        "validFromDate": f"{valid_from}T00:00:00.000Z",
                    }
                    for code, quantity, rate in entries
                ],
            }
        ]
    ).encode()


class _Response(io.BytesIO):
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Opener:
    """Answers by requested day, and notes every URL asked."""

    def __init__(self, answers: dict[str, bytes | Exception]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def open(self, url: str, timeout: float = 0) -> _Response:
        self.asked.append(url)
        day = url.split("date=")[1][:10]
        answer = self.answers[day]
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)


def test_each_day_keeps_the_rate_in_force_on_it() -> None:
    sat = _answer("2026-09-12", ("USD", 1, "2.6095"), ("JPY", 100, "1.8123"))
    # a Sunday's answer is the rate in force since Saturday
    rows = fetcher.rates_of_day(sat, date(2026, 9, 13))
    assert rows == [
        {"date": "2026-09-13", "currency": "USD", "quantity": 1, "rate": "2.6095"},
        {"date": "2026-09-13", "currency": "JPY", "quantity": 100, "rate": "1.8123"},
    ]


def test_a_day_not_published_yet_is_left_out_not_borrowed() -> None:
    old = _answer("2026-09-17", ("USD", 1, "2.6081"))
    assert fetcher.rates_of_day(old, date(2026, 10, 30)) == []  # weeks after the answer's day
    assert fetcher.rates_of_day(old, date(2026, 9, 16)) == []  # before it


@pytest.mark.parametrize(
    "answer",
    [
        b'{"date": "x"}',
        b"[]",
        b'[{"date": "2026-09-10"}]',
        b'[{"currencies": ["USD"]}]',
        _answer("2026-09-10", ("usd", 1, "2.6104")),
        _answer("2026-09-10", ("USD", 7, "2.6104")),
        _answer("2026-09-10", ("USD", 1, "-2.6104")),
        _answer("2026-09-10", ("USD", 1, "2.6104e3")),
        _answer("2026-09-10", ("USD", 1, "0")),
    ],
)
def test_an_answer_of_another_shape_stops_the_run(answer: bytes) -> None:
    with pytest.raises(ValueError, match=r"unexpected|no "):
        fetcher.rates_of_day(answer, date(2026, 9, 10))


def test_a_run_writes_a_file_the_server_imports(tmp_path: Path) -> None:
    opener = _Opener(
        {
            "2026-09-10": _answer("2026-09-10", ("USD", 1, "2.6104")),
            "2026-09-11": _answer("2026-09-11", ("USD", 1, "2.6110")),
        }
    )
    rates, empty = fetcher.fetch(
        date(2026, 9, 10), date(2026, 9, 11), ["USD"], delay=0, opener=opener
    )
    assert empty == []
    assert all("currencies=USD" in url and "nbg.gov.ge" in url for url in opener.asked)
    out = tmp_path / "nbg.json"
    digest = fetcher.write(out, rates, date(2026, 9, 10), date(2026, 9, 11))
    assert len(digest) == 64
    imported = parse_rates(out, today=date(2026, 10, 2))
    assert {(r.currency, r.valid_on.isoformat(), str(r.per_unit)) for r in imported} == {
        ("USD", "2026-09-10", "2.6104"),
        ("USD", "2026-09-11", "2.6110"),
    }
    assert not out.with_suffix(".json.tmp").exists()


def test_a_failing_day_is_retried_then_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetcher.time, "sleep", lambda _s: None)
    flaky = _Opener({"2026-09-10": urllib.error.URLError("timed out")})
    with pytest.raises(RuntimeError, match="2026-09-10"):
        fetcher.fetch(date(2026, 9, 10), date(2026, 9, 10), [], delay=0, opener=flaky)
    assert len(flaky.asked) == 3


def test_redirects_are_refused() -> None:
    handler = fetcher.NoRedirect()
    request: Any = None
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere") is None


def test_the_urls_name_only_a_day_and_currency_codes() -> None:
    url = fetcher.day_url(date(2026, 9, 10), ["USD", "EUR"])
    assert url == (
        "https://nbg.gov.ge/gw/api/ct/monetarypolicy/currencies/en/json/"
        "?date=2026-09-10&currencies=USD&currencies=EUR"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--from", "2026-09-10", "--to", "2026-09-01", "--out", "x.json"],
        ["--from", "10.09.2026", "--out", "x.json"],
        ["--from", "1990-01-01", "--to", "2026-01-01", "--out", "x.json"],
        ["--from", "2026-09-10", "--to", "2026-09-11", "--currencies", "usd$", "--out", "x"],
    ],
)
def test_bad_arguments_are_refused_before_any_request(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        fetcher.main(argv)
    assert exc.value.code == 2

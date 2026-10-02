"""What a gold file says the user gives the check besides the act and the contract: the parties'
registry extracts (rule 1), typed exchange rates and the advance (rule 9) - so a test set can
score those rules end to end, as the product runs them. Invented values only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from docx import Document
from pydantic import ValidationError

from procurement_agent.checks.findings import CheckReport
from procurement_agent.config import Settings
from procurement_agent.eval import runner
from procurement_agent.eval.gold import load_gold_set
from procurement_agent.registry.source import RegistrySource
from tests.unit.test_registry_extract import LAYOUT_2011


def _files(tmp_path: Path) -> tuple[Path, Path]:
    act = tmp_path / "act.docx"
    Document().save(str(act))
    extract = tmp_path / "extract.docx"
    doc = Document()
    for line in LAYOUT_2011.split("\n"):
        doc.add_paragraph(line)
    doc.save(str(extract))
    return act, extract


def _gold(tmp_path: Path, **extra: object) -> Path:
    folder = tmp_path / "gold"
    folder.mkdir(exist_ok=True)
    body = {"document": "act.docx", "check": "aoa", **extra}
    (folder / "aoa_x.yaml").write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return folder


def test_extracts_rates_and_the_advance_load_with_their_paths(tmp_path: Path) -> None:
    _files(tmp_path)
    folder = _gold(
        tmp_path,
        registry=["extract.docx"],
        rates=[{"currency": "USD", "date": "2026-09-10", "rate": "2.6104"}],
        advance={"amount": "1000", "date": "2026-09-10"},
    )
    [gold] = load_gold_set(folder, root=tmp_path).documents
    assert gold.registry == [tmp_path / "extract.docx"]
    assert [(r.currency, r.on, r.rate) for r in gold.rates] == [
        ("USD", date(2026, 9, 10), Decimal("2.6104"))
    ]
    assert gold.advance is not None
    assert (gold.advance.amount, gold.advance.paid_on) == (Decimal(1000), date(2026, 9, 10))


@pytest.mark.parametrize(
    "extra",
    [
        {"rates": [{"currency": "usd", "date": "2026-09-10", "rate": "2.6"}]},
        {"rates": [{"currency": "USD", "date": "2026-09-10", "rate": "0"}]},
        {"registry": ["missing.docx"]},
        {"advance": {"amount": "-1"}},
    ],
)
def test_a_wrong_input_is_refused_when_the_gold_loads(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    _files(tmp_path)
    with pytest.raises(ValidationError):
        load_gold_set(_gold(tmp_path, **extra), root=tmp_path)


def test_the_check_is_given_what_the_gold_says_the_user_gave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _files(tmp_path)
    folder = _gold(
        tmp_path,
        registry=["extract.docx"],
        rates=[{"currency": "USD", "date": "2026-09-10", "rate": "2.6104"}],
        advance={"amount": "1000", "date": "2026-09-10"},
    )
    [gold] = load_gold_set(folder, root=tmp_path).documents
    seen: dict[str, Any] = {}

    def capture(**kwargs: Any) -> CheckReport:
        seen.update(kwargs)
        rates = kwargs["rates"]
        seen["usd"] = rates.rate("USD", date(2026, 9, 10)) if rates is not None else None
        return CheckReport(document_id="x")

    monkeypatch.setattr(runner, "check_aoa", capture)
    runner._aoa_report(gold, [], Settings(env="dev"), tmp_path / "cache")
    registry = seen["registry"]
    assert isinstance(registry, RegistrySource)
    assert registry.lookup("400000017") is not None  # the extract, read as uploaded
    assert seen["usd"] is not None and seen["usd"].rate == Decimal("2.6104")
    assert (seen["advance"].amount, seen["advance"].paid_on) == (
        Decimal(1000),
        date(2026, 9, 10),
    )


def test_a_gold_without_them_runs_as_it_always_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _files(tmp_path)
    [gold] = load_gold_set(_gold(tmp_path), root=tmp_path).documents
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        runner, "check_aoa", lambda **kw: seen.update(kw) or CheckReport(document_id="x")
    )
    runner._aoa_report(gold, [], Settings(env="dev"), tmp_path / "cache")
    assert (seen["registry"], seen["rates"], seen["advance"]) == (None, None, None)

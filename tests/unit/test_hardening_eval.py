"""Regression tests for the evaluation harness, the registry loader and the Docling wrapper.

No Docling model is loaded here: conversion is replaced by a stub wherever a test only needs
to know *whether* it ran, which is all a cache test is about.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from procurement_agent.config import Settings
from procurement_agent.eval import runner
from procurement_agent.eval.score import values_match
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion import docling_service
from procurement_agent.registry.snapshot import (
    RegistryFormatError,
    load_snapshot,
    open_snapshot,
)

# --- scorer: no substring credit for numbers, dates or short fragments (audit 15) ----------------


@pytest.mark.parametrize(
    ("fact_type", "expected", "actual"),
    [
        (FactType.penalty, "5%", "15%"),
        (FactType.percentage, "5", "15"),
        (FactType.total_amount, "5", "15"),
        (FactType.warranty_period, "24", "124 months"),
        (FactType.contract_date, "2026-03-1", "2026-03-15"),
        (FactType.contract_date, "2026-03-15", "2026-03-15 and 2026-04-01"),
        (FactType.party_address, "თბილისი", "ქ. თბილისი, რუსთაველის გამზირი 12, მესამე სართული"),
    ],
)
def test_a_fragment_of_a_value_is_not_the_value(
    fact_type: FactType, expected: str, actual: str
) -> None:
    assert not values_match(fact_type, expected, actual)


@pytest.mark.parametrize(
    ("fact_type", "expected", "actual"),
    [
        (FactType.penalty, "0.1%", "0.1%"),
        (FactType.penalty, "0.1%", "0,1%"),
        (FactType.warranty_period, "24", "24 months"),
        (FactType.warranty_period, "24", "P24M"),
        (FactType.contract_date, "2026-03-15", "2026-03-15"),
        (FactType.total_amount, "48750.00", "48 750,00"),
        (FactType.project_name, "ლოფთ თაუნი", "ლოფთ თაუნის პროექტი"),
        (FactType.party_address, "ქ. თბილისი, რუსთაველის 12", "თბილისი, რუსთაველის 12"),
    ],
)
def test_the_same_value_written_differently_still_matches(
    fact_type: FactType, expected: str, actual: str
) -> None:
    assert values_match(fact_type, expected, actual)


# --- eval cache: keyed by everything that changes the parse, written atomically (audit 14) ------


def _stub_convert(calls: list[Settings], status: Any = None) -> Any:
    from docling.datamodel.base_models import ConversionStatus
    from docling_core.types.doc.document import DoclingDocument

    status = status or ConversionStatus.SUCCESS

    class _Result:
        def __init__(self) -> None:
            self.document = DoclingDocument(name="stub")
            self.status = status

    def convert(path: Path, settings: Settings) -> _Result:
        calls.append(settings)
        return _Result()

    return convert


def test_the_parse_cache_is_not_shared_across_ocr_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Settings] = []
    monkeypatch.setattr(runner, "convert", _stub_convert(calls))
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 synthetic")
    cache = tmp_path / "cache"

    runner.load_chunks_for(doc, Settings(ocr_langs=("kat", "eng")), cache)
    runner.load_chunks_for(doc, Settings(ocr_langs=("kat", "eng")), cache)
    assert len(calls) == 1  # same settings: served from the cache
    runner.load_chunks_for(doc, Settings(ocr_langs=("eng",)), cache)
    assert len(calls) == 2  # different OCR languages: a different parse


def test_a_partial_parse_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PARTIAL_SUCCESS (a timeout, a page that failed) must be retried, not kept forever."""
    from docling.datamodel.base_models import ConversionStatus

    calls: list[Settings] = []
    monkeypatch.setattr(runner, "convert", _stub_convert(calls, ConversionStatus.PARTIAL_SUCCESS))
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 synthetic")
    runner.load_chunks_for(doc, Settings(), tmp_path / "cache")
    runner.load_chunks_for(doc, Settings(), tmp_path / "cache")
    assert len(calls) == 2


def test_the_cache_key_names_what_changes_the_parse() -> None:
    base = docling_service.conversion_fingerprint(Settings())
    assert base != docling_service.conversion_fingerprint(Settings(ocr_langs=("eng",)))
    assert base != docling_service.conversion_fingerprint(
        Settings(docling_artifacts_path=Path("/elsewhere"))
    )
    assert base == docling_service.conversion_fingerprint(Settings())


def test_a_failed_cache_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docling_core.types.doc.document import DoclingDocument

    def half_write(self: DoclingDocument, filename: Path, **_: object) -> None:
        Path(filename).write_text('{"truncated": ', encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(DoclingDocument, "save_as_json", half_write)
    with pytest.raises(OSError, match="disk full"):
        docling_service.save_document(DoclingDocument(name="x"), tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


# --- converter singleton follows the settings (audit 25) ----------------------------------------


def test_the_converter_is_rebuilt_when_its_settings_change(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[str, ...]] = []

    def fake_build(settings: Settings) -> object:
        built.append(settings.ocr_langs)
        return object()

    monkeypatch.setattr(docling_service, "_build", fake_build)
    monkeypatch.setattr(docling_service, "_converters", {})
    a = docling_service.get_converter(Settings(ocr_langs=("kat", "eng")))
    assert docling_service.get_converter(Settings(ocr_langs=("kat", "eng"))) is a
    b = docling_service.get_converter(Settings(ocr_langs=("eng",)))
    assert b is not a
    assert built == [("kat", "eng"), ("eng",)]


# --- registry import (audit 24) ------------------------------------------------------------------


def _csv(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_a_failed_import_leaves_no_temporary_file(tmp_path: Path) -> None:
    bad = _csv(tmp_path / "bad.csv", "name,address\nშპს ნიმუში,თბილისი\n")
    db = tmp_path / "registry.db"
    with pytest.raises(RegistryFormatError):
        load_snapshot(bad, db)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.csv"]


def test_a_repeated_code_is_counted_once(tmp_path: Path) -> None:
    csv_path = _csv(
        tmp_path / "r.csv",
        "id_code,name\n204567890,შპს ალფა\n204567890,შპს ალფა (ახალი)\n405123456,შპს ბეტა\n",
    )
    db = tmp_path / "registry.db"
    assert load_snapshot(csv_path, db) == 2
    snap = open_snapshot(db)
    assert snap is not None
    with snap:
        assert snap.count == 2


def test_an_invalid_as_of_date_is_rejected(tmp_path: Path) -> None:
    csv_path = _csv(tmp_path / "r.csv", "id_code,name\n204567890,შპს ალფა\n")
    with pytest.raises(ValueError, match="as-of"):
        load_snapshot(csv_path, tmp_path / "registry.db", as_of="last tuesday")
    assert not (tmp_path / "registry.db").exists()


def test_the_snapshot_file_is_private_before_it_is_moved_into_place(tmp_path: Path) -> None:
    csv_path = _csv(tmp_path / "r.csv", "id_code,name\n204567890,შპს ალფა\n")
    db = tmp_path / "registry.db"
    load_snapshot(csv_path, db, as_of="2026-09-01")
    assert db.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM registry_meta").fetchone() == ("2026-09-01",)


def test_the_cli_rejects_an_invalid_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from procurement_agent import cli

    csv_path = _csv(tmp_path / "r.csv", "id_code,name\n204567890,შპს ალფა\n")
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["load-registry", str(csv_path), "--as-of", "2026-13-45"])
    assert exc.value.code == 2
    assert "as-of" in capsys.readouterr().err


def test_a_code_made_of_digits_is_compared_as_a_code() -> None:
    assert values_match(FactType.cadastral_code, "01.02.03.456", "01.02.03.456")
    assert not values_match(FactType.cadastral_code, "01.02.03.456", "01.02.03.4567")

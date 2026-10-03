"""A ``docling.json`` from the parser is stored by the atomic write ``save_document`` uses."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from docling_core.types.doc.document import DoclingDocument

from procurement_agent.ingestion import docling_service


def test_the_bytes_land_as_docling_json_with_owner_only_access(tmp_path: Path) -> None:
    out = tmp_path / "results" / "abc"
    path = docling_service.save_document_bytes(b'{"schema_name": "DoclingDocument"}', out)
    assert path == out / "docling.json"
    assert path.read_bytes() == b'{"schema_name": "DoclingDocument"}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert [p.name for p in out.iterdir()] == ["docling.json"], "no temporary file is left"


def test_it_replaces_an_earlier_parse_atomically(tmp_path: Path) -> None:
    docling_service.save_document_bytes(b"old", tmp_path)
    docling_service.save_document_bytes(b"new", tmp_path)
    assert (tmp_path / "docling.json").read_bytes() == b"new"


def test_a_failed_write_leaves_no_partial_file_and_keeps_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docling_service.save_document_bytes(b"old", tmp_path)

    def broken(self: Path, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", broken)
    with pytest.raises(OSError, match="disk full"):
        docling_service.save_document_bytes(b"new", tmp_path)
    monkeypatch.undo()
    assert (tmp_path / "docling.json").read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["docling.json"]


def test_save_document_writes_what_save_as_json_writes_through_the_same_helper(
    tmp_path: Path,
) -> None:
    doc = DoclingDocument(name="x")
    path = docling_service.save_document(doc, tmp_path / "a")
    expected = tmp_path / "direct.json"
    doc.save_as_json(expected)
    assert path.read_bytes() == expected.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

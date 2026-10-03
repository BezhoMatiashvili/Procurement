"""A checkpoint that fails, or that a reader keeps from finishing, is said in the log: the rows a
retention sweep removed stay readable in the write-ahead log until it works."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import structlog

import procurement_agent.db as dbmod
from procurement_agent.security.audit import configure_logging


class _Conn:
    def __init__(self, row: tuple[int, int, int] | None, boom: bool) -> None:
        self._row, self._boom = row, boom

    def __enter__(self) -> _Conn:
        if self._boom:
            raise RuntimeError("database is locked")
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def exec_driver_sql(self, sql: str) -> SimpleNamespace:
        return SimpleNamespace(fetchone=lambda: self._row)


class _Engine:
    def __init__(self, row: tuple[int, int, int] | None = (0, 5, 5), boom: bool = False) -> None:
        self._row, self._boom = row, boom

    def connect(self) -> _Conn:
        return _Conn(self._row, self._boom)


def _fresh_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """A logger bound now: the module's own was bound (and cached) by whichever test logged first,
    to the stdout of that test."""
    configure_logging(json_output=True)
    monkeypatch.setattr(dbmod, "_log", structlog.get_logger("procurement_agent"))


def _logged(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]


def test_a_failed_checkpoint_is_logged_without_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fresh_log(monkeypatch)
    monkeypatch.setattr(dbmod, "_engine", _Engine(boom=True))
    dbmod.checkpoint()
    events = [e for e in _logged(capsys) if e.get("event") == "checkpoint_failed"]
    assert events and events[0]["level"] == "warning"
    assert events[0]["error"] == "RuntimeError"
    assert "locked" not in json.dumps(events[0])


def test_a_busy_checkpoint_is_logged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fresh_log(monkeypatch)
    monkeypatch.setattr(dbmod, "_engine", _Engine(row=(1, 5, 2)))
    dbmod.checkpoint()
    assert any(e.get("event") == "checkpoint_busy" for e in _logged(capsys))


def test_a_clean_checkpoint_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fresh_log(monkeypatch)
    monkeypatch.setattr(dbmod, "_engine", _Engine(row=(0, 5, 5)))
    dbmod.checkpoint()
    assert not [e for e in _logged(capsys) if str(e.get("event", "")).startswith("checkpoint")]

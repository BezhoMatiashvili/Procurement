"""What is recorded when the egress guard refuses a connection: a host-shaped string, once."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from procurement_agent.security import audit as audit_mod


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Audit rows the reporter writes, captured instead of going to a database."""
    written: list[dict[str, Any]] = []
    done = threading.Event()

    def fake_audit_now(**kwargs: Any) -> None:
        written.append(kwargs)
        done.set()

    monkeypatch.setattr(audit_mod, "audit_now", fake_audit_now)
    monkeypatch.setattr(audit_mod, "_egress_seen", {})
    monkeypatch.setattr(audit_mod, "_egress_window", [0.0, 0])
    written.append({"_done": done})  # slot 0 carries the event; removed below
    yield written
    written.clear()


def _wait_for_rows(
    rows: list[dict[str, Any]], n: int, timeout: float = 5.0
) -> list[dict[str, Any]]:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(rows) - 1 < n:
        time.sleep(0.01)
    return rows[1:]


def test_a_blocked_connection_becomes_one_denied_audit_row(rows: list[dict[str, Any]]) -> None:
    audit_mod.report_blocked_egress("connect", "203.0.113.9")
    (row,) = _wait_for_rows(rows, 1)
    assert row["actor"] == "system"
    assert row["action"] == "egress.blocked"
    assert row["outcome"] == "denied"
    assert row["detail"] == "connect 203.0.113.9"


def test_the_same_destination_is_recorded_once_per_window(rows: list[dict[str, Any]]) -> None:
    for _ in range(50):
        audit_mod.report_blocked_egress("lookup", "telemetry.example.com")
    audit_mod.report_blocked_egress("connect", "telemetry.example.com")  # another kind: its own row
    got = _wait_for_rows(rows, 2)
    assert sorted(r["detail"] for r in got) == [
        "connect telemetry.example.com",
        "lookup telemetry.example.com",
    ]


def test_a_flood_of_distinct_destinations_is_capped(rows: list[dict[str, Any]]) -> None:
    """A document with thousands of links must not fill the audit table."""
    for i in range(500):
        audit_mod.report_blocked_egress("lookup", f"host{i}.example.com")
    got = _wait_for_rows(rows, audit_mod._EGRESS_ROWS_PER_WINDOW)
    import time

    time.sleep(0.2)  # let any stragglers land; there must not be any
    assert len(rows) - 1 == audit_mod._EGRESS_ROWS_PER_WINDOW
    assert len(got) <= audit_mod._EGRESS_ROWS_PER_WINDOW


def test_the_destination_is_cut_to_a_host_shaped_string(rows: list[dict[str, Any]]) -> None:
    """A link in a document picks the text; it must not carry control characters or length."""
    audit_mod.report_blocked_egress(
        "connect", 'evil.example\n{"injected": true} \x1b[31m' + "a" * 300
    )
    (row,) = _wait_for_rows(rows, 1)
    detail = row["detail"]
    assert "\n" not in detail
    assert "\x1b" not in detail
    assert " " not in detail.split(" ", 1)[1]
    assert len(detail) <= len("connect ") + 100


def test_a_failing_audit_write_does_not_break_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    done = threading.Event()

    def broken(**kwargs: Any) -> None:
        done.set()
        raise RuntimeError("database is locked")

    monkeypatch.setattr(audit_mod, "audit_now", broken)
    monkeypatch.setattr(audit_mod, "_egress_seen", {})
    monkeypatch.setattr(audit_mod, "_egress_window", [0.0, 0])
    audit_mod.report_blocked_egress("connect", "203.0.113.9")  # must return normally
    assert done.wait(5.0)


def test_the_reporter_logs_without_a_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_mod, "audit_now", lambda **_: None)
    monkeypatch.setattr(audit_mod, "_egress_seen", {})
    monkeypatch.setattr(audit_mod, "_egress_window", [0.0, 0])
    audit_mod.report_blocked_egress("send", "198.51.100.7")
    out = capsys.readouterr().out
    assert "egress_blocked" in out
    assert "198.51.100.7" in out

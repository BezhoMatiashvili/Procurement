"""A naive database time is UTC, and is written and shown as such."""

from __future__ import annotations

import json
from datetime import UTC, datetime


def test_a_naive_database_time_is_serialised_with_an_explicit_utc_offset() -> None:
    from procurement_agent.api.documents import DocumentOut

    naive = datetime(2026, 10, 3, 12, 30, 5)
    out = DocumentOut(
        id="a" * 32,
        name="x.pdf",
        kind="pdf",
        size_bytes=1,
        page_count=None,
        status="ready",  # type: ignore[arg-type]
        error=None,
        created_at=naive,
        expires_at=naive.replace(tzinfo=UTC),
    )
    data = json.loads(out.model_dump_json())
    assert data["created_at"] == "2026-10-03T12:30:05Z"
    assert data["expires_at"] == "2026-10-03T12:30:05Z"


def test_the_stamp_filter_labels_a_time_utc() -> None:
    from procurement_agent.ui.routes import _env

    stamp = _env.filters["utc"]
    assert stamp(datetime(2026, 10, 3, 12, 30)) == "2026-10-03 12:30 UTC"
    assert stamp(datetime(2026, 10, 3, 12, 30, tzinfo=UTC)) == "2026-10-03 12:30 UTC"
    assert stamp(None) == ""

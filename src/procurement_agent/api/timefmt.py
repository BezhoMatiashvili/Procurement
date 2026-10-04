"""Times as the API and the pages show them: always UTC, and said so.

SQLite stores a time without its zone, so a datetime read back is naive - and serialised as
``2026-10-03T12:30:05``, which a client reads as local time. Every time this application keeps
is UTC (``models._now``), so a naive one is UTC and is given the offset it was always missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer

__all__ = ["UtcTime", "as_utc", "stamp"]


def as_utc(value: datetime) -> datetime:
    """``value`` as a timezone-aware UTC time; a naive one is taken to be UTC already."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")


#: A datetime that is written to JSON as ISO 8601 with an explicit ``Z``.
UtcTime = Annotated[datetime, PlainSerializer(_iso, return_type=str, when_used="json")]


def stamp(value: datetime | None) -> str:
    """A time for a page: ``2026-10-03 12:30 UTC``; empty for none."""
    return "" if value is None else f"{as_utc(value):%Y-%m-%d %H:%M} UTC"

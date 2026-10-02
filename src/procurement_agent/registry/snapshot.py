"""Offline snapshot of the business registry (სამეწარმეო რეესტრი).

The client's first AOA rule is to identify each party by its identification code and then check
the company name, the signatories and the legal address against the registry. The system is
air-gapped, so there is no live lookup: the client exports a snapshot and the operator loads it.

The snapshot is reference data, not application data, so it lives in its own SQLite file that
can be replaced wholesale without touching uploads, jobs or the audit log. Every entry carries
the date the export was taken, because a check against a stale registry needs to say so.

When no snapshot is loaded the registry rules are *skipped and reported as skipped*, never
silently passed - see ``AoaChecker.rule_registry``.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from procurement_agent.extraction.normalize import normalize_spaces

#: Columns the export must provide. "directors" holds one or more names separated by ';'.
REQUIRED_COLUMNS = ("id_code", "name")
OPTIONAL_COLUMNS = ("address", "directors", "status", "as_of", "legal_form")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_company (
    id_code   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    address   TEXT,
    directors TEXT,
    status    TEXT,
    as_of     TEXT,
    legal_form TEXT
);
CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT);
"""

_NOT_DIGIT = re.compile(r"\D")


def normalize_code(value: str, *, restore_zero: bool = False) -> str:
    """An identification code as digits only: "406-444-789" and "406 444 789" are 406444789.

    ``restore_zero`` puts back the leading zero a spreadsheet drops from a code it took for a
    number: a company's code has nine digits and a person's eleven, so eight or ten digits are
    that code with its "0" lost. Only an export does that; a code read from a document is kept.
    """
    digits = _NOT_DIGIT.sub("", value or "")
    if restore_zero and len(digits) in (8, 10):
        digits = "0" + digits
    return digits


class RegistryFormatError(ValueError):
    """The export does not have the columns the loader needs."""


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    id_code: str
    name: str
    address: str | None = None
    directors: tuple[str, ...] = ()
    status: str | None = None
    as_of: str | None = None
    #: the legal form as the registry writes it ("შეზღუდული პასუხისმგებლობის საზოგადოება")
    legal_form: str | None = None
    #: where the entry comes from: the operator's "snapshot", or a party's uploaded "extract"
    source: str = "snapshot"
    #: each director's position where the registry gives it, in ``directors``' order
    positions: tuple[str, ...] = ()


def _split_directors(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(normalize_spaces(part) for part in value.split(";") if part.strip())


class RegistrySnapshot:
    """Read-only lookup over a loaded snapshot."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    @property
    def as_of(self) -> str | None:
        row = self._conn.execute("SELECT value FROM registry_meta WHERE key = 'as_of'").fetchone()
        return row["value"] if row else None

    @property
    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM registry_company").fetchone()["n"])

    def lookup(self, id_code: str) -> RegistryEntry | None:
        row = self._conn.execute(
            "SELECT * FROM registry_company WHERE id_code = ?", (normalize_code(id_code),)
        ).fetchone()
        if row is None:
            return None
        return RegistryEntry(
            id_code=row["id_code"],
            name=row["name"],
            address=row["address"],
            directors=_split_directors(row["directors"]),
            status=row["status"],
            as_of=row["as_of"] or self.as_of,
            legal_form=row["legal_form"] if "legal_form" in row.keys() else None,  # noqa: SIM118
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RegistrySnapshot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _rows(csv_path: Path) -> Iterator[dict[str, str]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            msg = (
                f"registry export is missing required column(s): {', '.join(missing)}. "
                f"Expected at least {', '.join(REQUIRED_COLUMNS)}; "
                f"optional: {', '.join(OPTIONAL_COLUMNS)}."
            )
            raise RegistryFormatError(msg)
        yield from reader


def parse_as_of(value: date | str | None) -> str:
    """The export date as ISO text, or today. Anything that is not a real date is refused.

    The date is shown next to every registry finding as "checked against the registry as of
    ...", so a typo here would put a false date in front of the client rather than fail.
    """
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        msg = f"as-of date must be an ISO date such as 2026-09-01, got {value!r}"
        raise ValueError(msg) from None


def load_snapshot(csv_path: Path, db_path: Path, *, as_of: date | str | None = None) -> int:
    """Build a snapshot database from a CSV export. Returns the number of companies loaded.

    The database is written whole and then moved into place, so a failed or partial import can
    never leave the checker consulting half a registry - and a failed one leaves nothing behind.
    The count is of distinct identification codes: an export that lists a company twice keeps
    the last row, and counting rows reported more companies than the snapshot holds.
    """
    stamp = parse_as_of(as_of)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        _write_snapshot(csv_path, tmp_path, stamp)
        # private before it becomes visible under its real name, not after
        tmp_path.chmod(0o600)
        tmp_path.replace(db_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    # sqlite3's own context manager commits but does not close; closing() does
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM registry_company").fetchone()[0])


def _write_snapshot(csv_path: Path, tmp_path: Path, stamp: str) -> None:
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(_SCHEMA)
        for row in _rows(csv_path):
            code = normalize_code(row.get("id_code") or "", restore_zero=True)
            name = normalize_spaces(row.get("name") or "")
            if not code or not name:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO registry_company"
                " (id_code, name, address, directors, status, as_of, legal_form)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    code,
                    name,
                    normalize_spaces(row.get("address") or "") or None,
                    (row.get("directors") or "").strip() or None,
                    normalize_spaces(row.get("status") or "") or None,
                    (row.get("as_of") or "").strip() or None,
                    normalize_spaces(row.get("legal_form") or "") or None,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO registry_meta (key, value) VALUES ('as_of', ?)", (stamp,)
        )
        conn.commit()
    finally:
        conn.close()


def open_snapshot(db_path: Path) -> RegistrySnapshot | None:
    """Open a snapshot if one has been loaded, otherwise None (the degraded mode)."""
    if not db_path.exists():
        return None
    return RegistrySnapshot(db_path)

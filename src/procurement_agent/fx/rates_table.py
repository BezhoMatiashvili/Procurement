"""The NBG's official exchange rates, imported from a file: the offline rate source of rule 9.

The National Bank of Georgia sets one official rate per currency every business day, valid from
the next calendar day; asked for any calendar day - a Sunday too - its public API answers with the
rate valid on that day. The server never goes online (docs/SECURITY.md), so the rates reach it as a
file: ``scripts/fetch_nbg_rates.py``, run on any connected machine, asks the NBG for every day of a
range and writes them down, and ``procurement-agent load-fx-rates`` imports the file here.

One row per currency per calendar day, exactly as the NBG answered for that day. A lookup is
therefore exact: a day no imported file covered has no rate - the check then names the day to
import or to type in - and a neighbouring day's rate is never borrowed, because which rate applies
on a holiday is the National Bank's to say, not ours to guess.

The rates live in a SQLite file of their own, like the registry snapshot: reference data an
operator extends without touching uploads, jobs or the audit trail. An import adds to what is
there; a day imported again keeps the newer file's rate, and the import counts how many changed.
Every value is checked before anything is written, and one bad row refuses the whole file: a rate
table that is half a file is worse than the one it replaced.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from procurement_agent.fx.provider import RateQuote

__all__ = [
    "MAX_FILE_BYTES",
    "QUANTITIES",
    "Coverage",
    "ImportResult",
    "RateRow",
    "RatesFormatError",
    "RatesTable",
    "TableRateProvider",
    "import_rates",
    "open_rates_table",
    "parse_rates",
]

#: The largest rates file read. Every NBG currency for thirty years is about 460,000 rows, a few
#: tens of megabytes as JSON; anything far beyond that is not a rates file.
MAX_FILE_BYTES = 64 * 1024 * 1024
#: The most rows one file may hold (see ``MAX_FILE_BYTES``).
MAX_ROWS = 2_000_000
#: The units the NBG quotes a currency in: one dollar, but a hundred yen and ten thousand rials.
QUANTITIES = frozenset({1, 10, 100, 1000, 10000})
#: The lari's first day. No official lari rate is older.
EARLIEST = date(1995, 9, 25)
#: How far past today a rate may be dated: the NBG publishes the next day's rate the evening
#: before, and a file fetched across midnight or a time zone may hold one more day. A date beyond
#: this is a typo, not a rate.
_FUTURE_DAYS = 3
#: Upper bound on a rate per quoted unit: no currency is worth a million lari.
_MAX_RATE = Decimal(1_000_000)
#: Decimal places a rate may carry: the NBG publishes four.
_MAX_PLACES = 6
_CURRENCY = re.compile(r"[A-Z]{3}")
#: The column names a CSV must carry, in any order.
_CSV_COLUMNS = frozenset({"date", "currency", "quantity", "rate"})
#: The local currency has no rate against itself.
_LOCAL = "GEL"
#: What a source name may be: "NBG", or a short word the operator chose.
_SOURCE = re.compile(r"[A-Za-z][A-Za-z0-9 ._-]{0,31}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_rate (
    currency    TEXT NOT NULL,
    valid_on    TEXT NOT NULL,
    rate        TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    source      TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (currency, valid_on)
);
"""


class RatesFormatError(ValueError):
    """The file is not a rates file this importer accepts; the message names the row and why."""


@dataclass(frozen=True, slots=True)
class RateRow:
    """One official rate as published: ``rate`` lari for ``quantity`` units on ``valid_on``."""

    currency: str
    valid_on: date
    rate: Decimal
    quantity: int
    source: str

    @property
    def per_unit(self) -> Decimal:
        """Lari for ONE unit - what an amount is multiplied by."""
        return self.rate / self.quantity


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What an import did, in counts and dates only (it is printed and audited)."""

    added: int
    changed: int
    unchanged: int
    currencies: tuple[str, ...]
    first: date | None
    last: date | None


# --- reading a file -------------------------------------------------------------------------


def _fail(where: str, why: str) -> RatesFormatError:
    return RatesFormatError(f"{where}: {why}")


def _day(value: object, where: str, latest: date) -> date:
    if not isinstance(value, str) or len(value) > 32:
        raise _fail(where, "the date must be an ISO date such as 2026-09-10")
    try:
        day = date.fromisoformat(value.strip())
    except ValueError:
        raise _fail(where, f"{value!r} is not an ISO date such as 2026-09-10") from None
    if not EARLIEST <= day <= latest:
        raise _fail(where, f"{day.isoformat()} is outside {EARLIEST.isoformat()} … {latest}")
    return day


def _currency(value: object, where: str) -> str:
    code = value.strip().upper() if isinstance(value, str) else ""
    if not _CURRENCY.fullmatch(code):
        raise _fail(where, "the currency must be a three-letter ISO 4217 code such as USD")
    if code == _LOCAL:
        raise _fail(where, "the lari has no exchange rate against itself")
    return code


def _quantity(value: object, where: str) -> int:
    if isinstance(value, bool):
        raise _fail(where, "the quantity must be a whole number")
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 6:
        value = int(value.strip())
    if not isinstance(value, int) or value not in QUANTITIES:
        allowed = ", ".join(str(q) for q in sorted(QUANTITIES))
        raise _fail(where, f"the quantity must be one of {allowed}")
    return value


def _rate(value: object, where: str) -> Decimal:
    # a JSON number arrives as a float: its shortest text, "2.6104", is the number the NBG wrote
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise _fail(where, "the rate must be a number such as 2.6104")
    text = value.strip() if isinstance(value, str) else repr(value)
    if not text or len(text) > 24:
        raise _fail(where, "the rate must be a number such as 2.6104")
    try:
        rate = Decimal(text)
    except InvalidOperation:
        raise _fail(where, f"{text!r} is not a number") from None
    if not rate.is_finite() or rate <= 0 or rate >= _MAX_RATE:
        raise _fail(where, f"{text} is not a usable exchange rate")
    exponent = rate.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_MAX_PLACES:
        raise _fail(where, f"{text} has more than {_MAX_PLACES} decimal places")
    return rate


def _source(value: object, where: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SOURCE.fullmatch(text):
        raise _fail(where, "the source must be a short name such as NBG")
    return text


def _rows_json(raw: bytes, latest: date) -> Iterator[RateRow]:
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _fail("file", f"not JSON: {e.__class__.__name__}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("rates"), list):
        raise _fail("file", 'expected an object {"source": "NBG", "rates": [ ... ]}')
    source = _source(doc.get("source", "NBG"), "source")
    rates: list[object] = doc["rates"]
    if len(rates) > MAX_ROWS:
        raise _fail("file", f"more than {MAX_ROWS} rates")
    for i, item in enumerate(rates, start=1):
        where = f"rate {i}"
        if not isinstance(item, dict):
            raise _fail(where, "each rate must be an object {date, currency, quantity, rate}")
        yield RateRow(
            currency=_currency(item.get("currency"), where),
            valid_on=_day(item.get("date"), where, latest),
            rate=_rate(item.get("rate"), where),
            quantity=_quantity(item.get("quantity", 1), where),
            source=source,
        )


def _rows_csv(raw: bytes, latest: date) -> Iterator[RateRow]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _fail("file", "a CSV rates file must be UTF-8") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = {c.strip().lower() for c in reader.fieldnames or []}
    if not columns >= _CSV_COLUMNS:
        raise _fail("header", "a CSV rates file needs the columns date,currency,quantity,rate")
    for i, row in enumerate(reader, start=2):
        if i - 1 > MAX_ROWS:
            raise _fail("file", f"more than {MAX_ROWS} rates")
        where = f"line {i}"
        fields = {k.strip().lower(): v for k, v in row.items() if isinstance(k, str)}
        yield RateRow(
            currency=_currency(fields.get("currency"), where),
            valid_on=_day(fields.get("date"), where, latest),
            rate=_rate(fields.get("rate"), where),
            quantity=_quantity(fields.get("quantity"), where),
            source="NBG",
        )


def parse_rates(path: Path, *, today: date | None = None) -> list[RateRow]:
    """Every rate in a rates file, checked; raises ``RatesFormatError`` naming the first bad row.

    JSON (what ``scripts/fetch_nbg_rates.py`` writes) or CSV (``date,currency,quantity,rate``),
    by the file's suffix. A day listed twice for one currency must say the same thing twice.
    """
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise _fail("file", f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    raw = path.read_bytes()
    latest = (today or datetime.now(UTC).date()) + timedelta(days=_FUTURE_DAYS)
    suffix = path.suffix.lower()
    if suffix == ".json":
        rows = list(_rows_json(raw, latest))
    elif suffix == ".csv":
        rows = list(_rows_csv(raw, latest))
    else:
        raise _fail("file", "a rates file must be .json or .csv")
    seen: dict[tuple[str, date], RateRow] = {}
    for row in rows:
        key = (row.currency, row.valid_on)
        before = seen.get(key)
        if before is not None and before.per_unit != row.per_unit:
            raise _fail(
                f"{row.currency} {row.valid_on.isoformat()}",
                "the file gives this day two different rates",
            )
        seen[key] = row
    return list(seen.values())


# --- the table --------------------------------------------------------------------------------


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        # private before it holds anything
        db_path.touch(mode=0o600)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def import_rates(rows: Iterable[RateRow], db_path: Path) -> ImportResult:
    """Add ``rows`` to the table at ``db_path``, in one transaction: all of them or none."""
    rows = list(rows)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    added = changed = unchanged = 0
    with closing(_connect_rw(db_path)) as conn, conn:
        for row in rows:
            current = conn.execute(
                "SELECT rate, quantity FROM fx_rate WHERE currency = ? AND valid_on = ?",
                (row.currency, row.valid_on.isoformat()),
            ).fetchone()
            if current is None:
                added += 1
            elif Decimal(current[0]) / int(current[1]) == row.per_unit:
                unchanged += 1
                continue
            else:
                changed += 1
            conn.execute(
                "INSERT OR REPLACE INTO fx_rate"
                " (currency, valid_on, rate, quantity, source, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row.currency,
                    row.valid_on.isoformat(),
                    str(row.rate),
                    row.quantity,
                    row.source,
                    stamp,
                ),
            )
    days = [r.valid_on for r in rows]
    return ImportResult(
        added=added,
        changed=changed,
        unchanged=unchanged,
        currencies=tuple(sorted({r.currency for r in rows})),
        first=min(days, default=None),
        last=max(days, default=None),
    )


@dataclass(frozen=True, slots=True)
class Coverage:
    """The days a currency has rates for: the first, the last, and how many days are held."""

    currency: str
    first: date
    last: date
    days: int

    @property
    def missing(self) -> int:
        """Days between the first and the last that have no rate."""
        return (self.last - self.first).days + 1 - self.days


class RatesTable:
    """Read-only lookups over an imported rates table."""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)

    def row(self, currency: str, on: date) -> RateRow | None:
        found = self._conn.execute(
            "SELECT rate, quantity, source FROM fx_rate WHERE currency = ? AND valid_on = ?",
            (currency.upper(), on.isoformat()),
        ).fetchone()
        if found is None:
            return None
        return RateRow(
            currency=currency.upper(),
            valid_on=on,
            rate=Decimal(found[0]),
            quantity=int(found[1]),
            source=str(found[2]),
        )

    def coverage(self, currency: str | None = None) -> list[Coverage]:
        """Per currency (or the one asked for): first and last day and the count of days held."""
        query = "SELECT currency, MIN(valid_on), MAX(valid_on), COUNT(*) FROM fx_rate"
        args: tuple[str, ...] = ()
        if currency is not None:
            query += " WHERE currency = ?"
            args = (currency.upper(),)
        query += " GROUP BY currency ORDER BY currency"
        return [
            Coverage(c, date.fromisoformat(a), date.fromisoformat(b), int(n))
            for c, a, b, n in self._conn.execute(query, args).fetchall()
        ]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RatesTable:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_rates_table(db_path: Path) -> RatesTable | None:
    """The imported table, or None when nothing was ever imported."""
    if not db_path.is_file():
        return None
    table = RatesTable(db_path)
    try:
        table.coverage()  # a file that is no rates table fails here, not in the middle of a check
    except sqlite3.DatabaseError:
        table.close()
        return None
    return table


class TableRateProvider:
    """``RateProvider`` over the imported table (``Settings.fx_rate_source = "table"``).

    With nothing imported it simply has no rate for any day, like ``NullRateProvider``, and the
    check names the days to import or to type in.
    """

    def __init__(self, db_path: Path) -> None:
        self._table = open_rates_table(db_path)

    @property
    def loaded(self) -> bool:
        return self._table is not None

    def rate(self, currency: str, on: date) -> RateQuote | None:
        if self._table is None:
            return None
        row = self._table.row(currency, on)
        if row is None:
            return None
        return RateQuote(currency=row.currency, rate=row.per_unit, valid_on=on, source=row.source)

    def coverage(self, currency: str) -> tuple[date, date] | None:
        if self._table is None:
            return None
        found = self._table.coverage(currency)
        return (found[0].first, found[0].last) if found else None

    def close(self) -> None:
        if self._table is not None:
            self._table.close()

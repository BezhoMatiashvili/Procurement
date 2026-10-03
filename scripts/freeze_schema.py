#!/usr/bin/env python
"""Record the schema a new database gets, as the migration tests hold it.

    python scripts/freeze_schema.py tests/fixtures/schema_v2.json

Run it when a migration is added, with the models and ``MIGRATIONS`` as they are to be released,
and commit the file beside the migration: ``tests/unit/test_migrations.py`` compares a new
database with ``schema_v<LATEST_VERSION>.json`` byte for byte, and an upgraded one with a new one.
``schema_v1.json`` was made the same way from the schema ``create_all`` had always built.

It creates a database in a temporary directory and reads ``sqlite_master``; no document, no
setting and no existing database is read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from procurement_agent import migrations  # noqa: E402
from procurement_agent.db import init_db  # noqa: E402


def schema_of_a_new_database() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "data" / "app.db"
        init_db(path).dispose()
        con = sqlite3.connect(path)
        try:
            rows = con.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        finally:
            con.close()
    objects = [{"type": t, "name": n, "table": tb, "sql": s} for t, n, tb, s in rows]
    return {"version": migrations.LATEST_VERSION, "objects": objects}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: freeze_schema.py tests/fixtures/schema_v<N>.json", file=sys.stderr)
        return 2
    text = json.dumps(schema_of_a_new_database(), indent=1, ensure_ascii=False) + "\n"
    Path(args[0]).write_text(text, encoding="utf-8")
    print(f"schema version {migrations.LATEST_VERSION}: {args[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

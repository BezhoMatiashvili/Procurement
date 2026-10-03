"""Schema versions and migrations: what a start does to a database an earlier release wrote.

The schema version lives in SQLite's own ``PRAGMA user_version``. A new database is created at the
latest version; one written before versions existed (version 0) is checked against the frozen shape
of version 1 and adopted; one that is behind has the pending migrations applied, each in its own
transaction; one that is *ahead* is refused. The schema the frozen fixture records,
``tests/fixtures/schema_v1.json``, is what ``init_db`` built before any of this existed.

Every migration used here is a test's own: the real registry holds none beyond the baseline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

import procurement_agent.db as dbmod
from procurement_agent import cli, migrations
from procurement_agent.config import get_settings
from procurement_agent.migrations import (
    BASELINE_SCHEMA,
    LATEST_VERSION,
    Migration,
    MigrationError,
    apply_migrations,
    inspect_database,
)
from procurement_agent.security import secure_delete
from procurement_agent.security.safe_errors import safe_exc

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
CANARY = "CANARY-5d1f-confidential-text"


# --- building databases ------------------------------------------------------------------------


def _snapshot(version: int = 1) -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / f"schema_v{version}.json").read_text(encoding="utf-8"))
    objects: list[dict[str, Any]] = data["objects"]
    return objects


#: A few rows in the tables a release before versions had in use. The canary sits where a real
#: file name and a typed question would.
_SEED = (
    (
        "INSERT INTO api_keys (id, user, key_hash, prefix, created_at) "
        "VALUES ('k1', 'alice', ?, 'pa_abc', '2026-01-01 00:00:00.000000')",
        ("a" * 64,),
    ),
    (
        "INSERT INTO documents (id, owner, display_name, kind, size_bytes, sha256, status, "
        "created_at) VALUES "
        "('d1', 'alice', ?, 'docx', 10, ?, 'ready', '2026-01-01 00:00:00.000000')",
        (f"{CANARY}.docx", "b" * 64),
    ),
    (
        "INSERT INTO jobs (id, document_id, owner, kind, params, status, progress, created_at) "
        "VALUES ('j1', 'd1', 'alice', 'extract', ?, 'done', 100, '2026-01-01 00:00:00.000000')",
        (json.dumps({"question": CANARY}),),
    ),
    (
        "INSERT INTO audit_log (at, actor, action, outcome, detail) "
        "VALUES ('2026-01-01 00:00:00.000000', 'alice', 'upload', 'ok', 'document d1')",
        (),
    ),
)
_SEEDED_TABLES = ("api_keys", "documents", "jobs", "audit_log")


def build_legacy(
    path: Path, *, without: tuple[str, ...] = (), rows: bool = True, triggers: bool = True
) -> Path:
    """A database as the release before schema versions left it: the frozen schema, version 0."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    order = {"table": 0, "index": 1, "trigger": 2}
    for obj in sorted(_snapshot(), key=lambda o: order[o["type"]]):
        skip = obj["sql"] is None or obj["table"] in without
        if skip or (obj["type"] == "trigger" and not triggers):
            continue
        con.execute(obj["sql"])
    if rows:
        for sql, params in _SEED:
            con.execute(sql, params)
    con.commit()
    con.close()
    return path


def sql_rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def user_version(path: Path) -> int:
    return int(sql_rows(path, "PRAGMA user_version")[0][0])


def seeded_rows(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    return {t: sql_rows(path, f"SELECT * FROM {t} ORDER BY 1") for t in _SEEDED_TABLES}


def master(path: Path) -> list[tuple[Any, ...]]:
    return sql_rows(path, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")


def columns(path: Path, table: str) -> list[str]:
    return [r[0] for r in sql_rows(path, "SELECT name FROM pragma_table_info(?)", (table,))]


def tables(path: Path) -> set[str]:
    rows = sql_rows(path, "SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r[0] for r in rows}


def shape(path: Path) -> dict[str, Any]:
    """Everything about a schema that behaviour depends on, independent of how its DDL was
    written: ``ALTER TABLE ... ADD COLUMN`` and ``CREATE TABLE`` spell one table differently."""
    out: dict[str, Any] = {}
    for table in sorted(tables(path)):
        if table.startswith("sqlite_"):
            continue
        indexes = {}
        for _, name, unique, *_ in sql_rows(path, "SELECT * FROM pragma_index_list(?)", (table,)):
            cols = sql_rows(path, "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,))
            indexes[name] = (unique, [c[0] for c in cols])
        out[table] = {
            "columns": sql_rows(
                path,
                'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?)',
                (table,),
            ),
            "indexes": indexes,
            "foreign_keys": sql_rows(
                path,
                'SELECT "from", "table", "to", on_delete FROM pragma_foreign_key_list(?)',
                (table,),
            ),
        }
    out["triggers"] = sorted(
        r[0] for r in sql_rows(path, "SELECT name FROM sqlite_master WHERE type = 'trigger'")
    )
    return out


def engine_for(path: Path) -> Engine:
    return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})


def apply(path: Path, registry: tuple[Migration, ...] | None = None, **kw: Any) -> Any:
    engine = engine_for(path)
    try:
        return apply_migrations(engine, path, registry=registry, **kw)
    finally:
        engine.dispose()


def at_version_one(path: Path, *, rows: bool = True) -> Path:
    """A database that stands at version 1 whatever the real registry says."""
    build_legacy(path, rows=rows)
    result = apply(path, registry=())
    assert (result.from_version, result.to_version) == (0, 1)
    return path


# --- migrations of the tests' own --------------------------------------------------------------


def _add_note(conn: Connection) -> None:
    conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN note VARCHAR(255)")


def _add_probe(conn: Connection) -> None:
    conn.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY, label VARCHAR(20))")
    conn.exec_driver_sql("INSERT INTO probe (label) VALUES ('seeded')")


def registry(
    ran: list[int] | None = None, *, failing: bool = False, pause: float = 0.0
) -> tuple[Migration, ...]:
    """Versions 2, 3 and 4. Version 3 can leave half of its work behind and then raise."""

    def record(version: int) -> None:
        if ran is not None:
            ran.append(version)

    def two(conn: Connection) -> None:
        record(2)
        time.sleep(pause)
        _add_note(conn)

    def three(conn: Connection) -> None:
        record(3)
        if failing:
            conn.exec_driver_sql("CREATE TABLE half_done (x INTEGER)")
            conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN leaked INTEGER")
            raise RuntimeError(f"could not convert {CANARY}")
        _add_probe(conn)

    def four(conn: Connection) -> None:
        record(4)
        conn.exec_driver_sql("CREATE TABLE after_failure (x INTEGER)")

    return (
        Migration(2, "add_note", two),
        Migration(3, "add_probe", three),
        Migration(4, "after_failure", four),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture(autouse=True)
def _no_engine_left_behind() -> Iterator[None]:
    yield
    dbmod._engine = None
    dbmod._SessionLocal = None


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def concurrently[T](jobs: list[Callable[[], T]]) -> list[T]:
    barrier = threading.Barrier(len(jobs))
    out: list[Any] = [None] * len(jobs)
    errors: list[BaseException] = []

    def run(i: int, job: Callable[[], T]) -> None:
        try:
            barrier.wait(timeout=30)
            out[i] = job()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    if errors:
        raise errors[0]
    return out


# --- the registry ------------------------------------------------------------------------------


def test_the_real_registry_is_in_order_and_the_latest_version_follows_from_it() -> None:
    migrations.check_registry(migrations.MIGRATIONS)
    assert [m.version for m in migrations.MIGRATIONS] == list(
        range(2, 2 + len(migrations.MIGRATIONS))
    )
    assert LATEST_VERSION == migrations.latest_version() == 1 + len(migrations.MIGRATIONS)
    assert migrations.latest_version(()) == 1  # the baseline alone
    assert migrations.latest_version(registry()) == 4


@pytest.mark.parametrize(
    "bad",
    [
        (Migration(3, "starts_late", _add_note),),
        (Migration(2, "first", _add_note), Migration(4, "gap", _add_probe)),
        (Migration(3, "out_of_order", _add_note), Migration(2, "second", _add_probe)),
        (Migration(2, "twice", _add_note), Migration(3, "twice", _add_probe)),
        (Migration(2, "Not An Identifier!", _add_note),),
        (Migration(2, "", _add_note),),
        (Migration(2, "x" * 41, _add_note),),
    ],
)
def test_a_registry_with_a_gap_a_repeat_or_an_odd_name_is_refused_before_the_database_is_touched(
    bad: tuple[Migration, ...], db_path: Path
) -> None:
    with pytest.raises(ValueError, match="registry"):
        migrations.check_registry(bad)
    with pytest.raises(ValueError, match="registry"):
        apply(db_path, bad)
    assert not db_path.exists()


def test_the_baseline_is_the_frozen_schema_not_whatever_the_models_say_today() -> None:
    con = sqlite3.connect(":memory:")
    for obj in _snapshot():
        if obj["type"] == "table":
            con.execute(obj["sql"])
    frozen = {
        t: tuple(r[1] for r in con.execute(f"PRAGMA table_info({t})"))
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert dict(BASELINE_SCHEMA) == frozen


# --- a new database ----------------------------------------------------------------------------


def test_a_new_database_is_created_at_the_latest_version_and_needs_no_backup(
    db_path: Path,
) -> None:
    db_path.parent.mkdir()
    result = apply(db_path)
    assert (result.from_version, result.to_version) == (0, LATEST_VERSION)
    assert result.applied == () and result.backup is None
    assert user_version(db_path) == LATEST_VERSION
    assert not (db_path.parent / "db-backups").exists()
    assert {"audit_log_no_update", "audit_log_no_delete"} <= set(shape(db_path)["triggers"])


def test_the_schema_a_new_database_gets_is_the_recorded_one_byte_for_byte(db_path: Path) -> None:
    """``schema_v<N>.json`` is what ``init_db`` built when version N was current. A change to a
    model that is not also a migration, or a migration that is not recorded, fails here."""
    snapshot = FIXTURES / f"schema_v{LATEST_VERSION}.json"
    assert snapshot.exists(), f"record the schema of version {LATEST_VERSION} in {snapshot.name}"
    dbmod.init_db(db_path).dispose()
    expected = [(o["type"], o["name"], o["table"], o["sql"]) for o in _snapshot(LATEST_VERSION)]
    assert master(db_path) == sorted(expected, key=lambda r: (r[0], r[1]))


def test_the_script_that_records_the_schema_reproduces_the_committed_record(
    tmp_path: Path,
) -> None:
    script = ROOT / "scripts" / "freeze_schema.py"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PA_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = tmp_path / "schema.json"
    done = subprocess.run(
        [sys.executable, str(script), str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    recorded = json.loads((FIXTURES / f"schema_v{LATEST_VERSION}.json").read_text(encoding="utf-8"))
    assert json.loads(out.read_text(encoding="utf-8")) == recorded
    assert recorded["version"] == LATEST_VERSION
    usage = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert usage.returncode == 2 and "usage" in usage.stderr


def test_a_new_database_skips_every_pending_migration_by_being_created_current(
    db_path: Path,
) -> None:
    db_path.parent.mkdir()
    ran: list[int] = []
    result = apply(db_path, registry(ran))
    assert (result.from_version, result.to_version, result.applied) == (0, 4, ())
    assert ran == [] and user_version(db_path) == 4
    assert "note" not in columns(db_path, "documents")  # the models, not the test migrations


def test_upgrading_a_version_one_database_gives_the_schema_a_new_one_has(
    tmp_path: Path,
) -> None:
    """The drift guard: whatever the migrations do, the end of the road is the models' schema."""
    upgraded = at_version_one(tmp_path / "up" / "app.db", rows=False)
    apply(upgraded)  # the real registry, up to the latest
    fresh = tmp_path / "new" / "app.db"
    fresh.parent.mkdir()
    apply(fresh)
    assert user_version(upgraded) == user_version(fresh) == LATEST_VERSION
    assert shape(upgraded) == shape(fresh)


# --- a database from before versions -----------------------------------------------------------


def test_a_database_from_before_versions_is_adopted_at_version_one_without_losing_a_row(
    db_path: Path,
) -> None:
    build_legacy(db_path)
    before = seeded_rows(db_path)
    assert all(before[t] for t in _SEEDED_TABLES) and user_version(db_path) == 0
    result = apply(db_path, registry=())
    assert (result.from_version, result.to_version) == (0, 1)
    assert result.applied == ("baseline",) and result.backup is None  # stamping needs no copy
    assert user_version(db_path) == 1
    assert seeded_rows(db_path) == before


def test_an_adopted_database_still_refuses_to_rewrite_the_audit_trail(db_path: Path) -> None:
    build_legacy(db_path)
    dbmod.init_db(db_path).dispose()
    con = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE audit_log SET detail = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM audit_log")
    con.close()
    assert seeded_rows(db_path)["audit_log"][0][-1] == "document d1"


def test_the_triggers_a_legacy_database_lacked_are_installed_on_start(db_path: Path) -> None:
    build_legacy(db_path, triggers=False)
    assert shape(db_path)["triggers"] == []
    dbmod.init_db(db_path).dispose()
    assert sorted(shape(db_path)["triggers"]) == ["audit_log_no_delete", "audit_log_no_update"]


def test_tables_an_older_release_never_had_are_added_to_a_legacy_database(db_path: Path) -> None:
    gone = ("check_runs", "check_run_documents", "ui_sessions")
    build_legacy(db_path, without=gone)
    before = seeded_rows(db_path)
    assert not set(gone) & tables(db_path)
    apply(db_path, registry=())
    assert set(gone) <= tables(db_path)
    assert seeded_rows(db_path) == before and user_version(db_path) == 1


def test_a_legacy_database_continues_with_the_pending_migrations_after_adoption(
    db_path: Path,
) -> None:
    build_legacy(db_path)
    ran: list[int] = []
    result = apply(db_path, registry(ran))
    assert (result.from_version, result.to_version) == (0, 4)
    assert result.applied == ("baseline", "add_note", "add_probe", "after_failure")
    assert ran == [2, 3, 4] and user_version(db_path) == 4


def test_a_legacy_database_is_checked_against_version_one_not_against_later_models(
    db_path: Path,
) -> None:
    """The column ``add_note`` adds is not in a version-0 database, and must not be asked for."""
    build_legacy(db_path)
    apply(db_path, registry()[:1])
    assert "note" in columns(db_path, "documents")


@pytest.mark.parametrize(
    ("damage", "needle"),
    [
        ("ALTER TABLE documents DROP COLUMN error", "documents has no column error"),
        ("ALTER TABLE jobs ADD COLUMN extra_notes TEXT", "jobs has 1 column"),
        ("DROP TABLE ui_sessions; CREATE VIEW ui_sessions AS SELECT 1 AS id", "ui_sessions"),
    ],
)
def test_a_legacy_database_of_another_shape_is_refused_and_left_as_it_was(
    db_path: Path, damage: str, needle: str
) -> None:
    build_legacy(db_path, without=("check_runs",))
    con = sqlite3.connect(db_path)
    con.executescript(damage)
    con.close()
    before, rows = master(db_path), seeded_rows(db_path)
    with pytest.raises(MigrationError, match="does not match this release") as caught:
        apply(db_path)
    assert needle in str(caught.value) and len(safe_exc(caught.value)) < 200
    assert master(db_path) == before and seeded_rows(db_path) == rows
    assert user_version(db_path) == 0 and "check_runs" not in tables(db_path)


def test_a_refusal_names_our_tables_and_columns_and_never_what_the_file_holds(
    db_path: Path,
) -> None:
    build_legacy(db_path)
    con = sqlite3.connect(db_path)
    con.execute(f'ALTER TABLE jobs ADD COLUMN "{CANARY}" TEXT')
    con.close()
    with pytest.raises(MigrationError) as caught:
        apply(db_path)
    assert CANARY not in str(caught.value) and CANARY not in safe_exc(caught.value)
    assert str(db_path.parent) not in str(caught.value)
    assert "jobs" in str(caught.value)


# --- pending migrations ------------------------------------------------------------------------


def test_pending_migrations_run_in_order_and_are_recorded(db_path: Path) -> None:
    at_version_one(db_path)
    before = seeded_rows(db_path)
    ran: list[int] = []
    result = apply(db_path, registry(ran))
    assert (result.from_version, result.to_version) == (1, 4)
    assert result.applied == ("add_note", "add_probe", "after_failure")
    assert ran == [2, 3, 4] and user_version(db_path) == 4
    assert "note" in columns(db_path, "documents")
    assert sql_rows(db_path, "SELECT label FROM probe") == [("seeded",)]
    document = seeded_rows(db_path)["documents"][0]
    assert document[: len(before["documents"][0])] == before["documents"][0]
    assert document[-1] is None, "the new column, empty"


def test_only_the_migrations_after_the_current_version_run(db_path: Path) -> None:
    at_version_one(db_path)
    apply(db_path, registry()[:1])  # now at 2
    ran: list[int] = []
    result = apply(db_path, registry(ran))
    assert (result.from_version, result.applied) == (2, ("add_probe", "after_failure"))
    assert ran == [3, 4]


def test_a_failing_migration_undoes_its_own_step_and_stops_there(db_path: Path) -> None:
    at_version_one(db_path)
    ran: list[int] = []
    with pytest.raises(MigrationError) as caught:
        apply(db_path, registry(ran, failing=True))
    message = str(caught.value)
    assert "migration 3 (add_probe)" in message and "rolled back" in message
    assert "RuntimeError" in message and "version 2" in message
    assert CANARY not in message and CANARY not in safe_exc(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert ran == [2, 3], "the migration after the failing one never ran"
    assert user_version(db_path) == 2, "the database stays at the last good version"
    assert "note" in columns(db_path, "documents"), "the step before it stays"
    assert "half_done" not in tables(db_path), "the failing step's DDL is gone"
    assert "leaked" not in columns(db_path, "jobs")
    assert "after_failure" not in tables(db_path)
    # and the next start, with the migration fixed, picks up where it stopped
    result = apply(db_path, registry(ran))
    assert (result.from_version, result.applied) == (2, ("add_probe", "after_failure"))


def test_a_migration_that_leaves_a_broken_foreign_key_is_undone(db_path: Path) -> None:
    at_version_one(db_path)

    def orphan(conn: Connection) -> None:
        conn.exec_driver_sql(
            "INSERT INTO jobs (id, document_id, owner, kind, params, status, progress, "
            "created_at) VALUES ('j9', 'no-such-document', 'u', 'x', '{}', 'done', 0, '2026')"
        )

    with pytest.raises(MigrationError, match="foreign key") as caught:
        apply(db_path, (Migration(2, "orphan", orphan),))
    assert "migration 2 (orphan)" in str(caught.value) and "1 row" in str(caught.value)
    assert user_version(db_path) == 1
    assert sql_rows(db_path, "SELECT count(*) FROM jobs WHERE id = 'j9'") == [(0,)]


def test_rebuilding_a_table_in_a_migration_does_not_cascade_into_its_children(
    db_path: Path,
) -> None:
    """The reason the migration connection has foreign keys off: with them on, dropping the old
    ``documents`` would delete every job that belongs to one."""
    at_version_one(db_path)

    def rebuild(conn: Connection) -> None:
        ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'documents'")
        conn.exec_driver_sql(
            ddl.scalar_one().replace("CREATE TABLE documents", "CREATE TABLE documents_new", 1)
        )
        conn.exec_driver_sql("INSERT INTO documents_new SELECT * FROM documents")
        conn.exec_driver_sql("DROP TABLE documents")
        conn.exec_driver_sql("ALTER TABLE documents_new RENAME TO documents")

    before = seeded_rows(db_path)
    apply(db_path, (Migration(2, "rebuild_documents", rebuild),))
    assert user_version(db_path) == 2
    assert seeded_rows(db_path)["jobs"] == before["jobs"]


def test_the_second_start_changes_nothing(db_path: Path) -> None:
    at_version_one(db_path)
    ran: list[int] = []
    first = apply(db_path, registry(ran))
    assert first.backup is not None
    backups = sorted((db_path.parent / "db-backups").iterdir())
    again = apply(db_path, registry(ran))
    assert (again.from_version, again.to_version) == (4, 4)
    assert again.applied == () and again.backup is None
    assert ran == [2, 3, 4]
    assert sorted((db_path.parent / "db-backups").iterdir()) == backups


def test_a_database_newer_than_the_release_is_refused_and_not_touched(db_path: Path) -> None:
    at_version_one(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 9")
    con.close()
    before = master(db_path)
    with pytest.raises(MigrationError, match=r"version 9 is newer than this release \(4\)"):
        apply(db_path, registry())
    assert master(db_path) == before and user_version(db_path) == 9
    assert not (db_path.parent / "db-backups").exists()


def test_an_empty_file_stamped_with_a_newer_version_gets_no_tables(db_path: Path) -> None:
    db_path.parent.mkdir()
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 7")
    con.close()
    with pytest.raises(MigrationError, match="newer than this release"):
        apply(db_path)
    assert tables(db_path) == set()


def test_what_a_migration_logs_is_its_version_and_name_and_nothing_else(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def info(self, event: str, **fields: Any) -> None:
            self.events.append((event, fields))

    recorder = Recorder()
    monkeypatch.setattr(migrations, "log", recorder)
    at_version_one(db_path)
    apply(db_path, registry())
    assert recorder.events[-3:] == [
        ("db_migration_applied", {"version": 2, "migration": "add_note"}),
        ("db_migration_applied", {"version": 3, "migration": "add_probe"}),
        ("db_migration_applied", {"version": 4, "migration": "after_failure"}),
    ]
    shown = repr(recorder.events)
    assert CANARY not in shown and str(db_path.parent) not in shown


# --- the copy taken before an upgrade ----------------------------------------------------------


def test_a_consistent_copy_is_taken_before_the_first_migration_of_a_database_with_data(
    db_path: Path,
) -> None:
    at_version_one(db_path)
    before = seeded_rows(db_path)
    old = os.umask(0o022)  # an ordinary umask: nothing here may rely on the process being hardened
    try:
        result = apply(db_path, registry())
    finally:
        os.umask(old)
    backups = db_path.parent / "db-backups"
    assert result.backup is not None and result.backup.parent == backups
    assert result.backup.name.startswith("app-v1-") and result.backup.suffix == ".db"
    assert mode(backups) == 0o700 and mode(result.backup) == 0o600
    assert [p.name for p in backups.iterdir()] == [result.backup.name], "one plain file, no -wal"
    assert sql_rows(result.backup, "PRAGMA integrity_check") == [("ok",)]
    assert user_version(result.backup) == 1, "the copy is of the version it was taken at"
    assert "note" not in columns(result.backup, "documents"), "taken before the first step"
    assert seeded_rows(result.backup) == before
    assert sql_rows(result.backup, "PRAGMA journal_mode") == [("delete",)]


def test_the_copy_of_a_legacy_database_is_taken_before_it_is_adopted(db_path: Path) -> None:
    build_legacy(db_path, without=("ui_sessions",))
    result = apply(db_path, registry())
    assert result.backup is not None and result.backup.name.startswith("app-v0-")
    assert user_version(result.backup) == 0
    assert "ui_sessions" not in tables(result.backup) and "ui_sessions" in tables(db_path)


def test_the_copy_and_the_look_survive_an_awkward_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both open the file through a URI built from its path: a ``?``, a ``#`` or a ``%`` in a
    directory name must not turn into a query string, a fragment or an escape."""
    awkward = tmp_path / "data dir #1 ? 100%"
    build_legacy(awkward / "app.db")
    monkeypatch.chdir(tmp_path)
    relative = Path(awkward.name) / "app.db"
    assert inspect_database(relative, ()).state == "legacy"
    copy = migrations._take_backup(relative, 0)
    assert copy.exists() and copy.parent.name == "db-backups" and user_version(copy) == 0
    assert seeded_rows(copy) == seeded_rows(awkward / "app.db")


def test_a_relative_data_directory_upgrades_with_its_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PA_DATA_DIR`` defaults to ``./data``, and ``Path.as_uri`` refuses a relative path."""
    monkeypatch.chdir(tmp_path)
    relative = Path("data") / "app.db"
    at_version_one(relative)
    result = apply(relative, registry())
    assert result.backup is not None and result.backup.exists()
    assert user_version(result.backup) == 1 and user_version(relative) == 4
    assert inspect_database(relative, registry()).version == 4


def test_an_existing_backup_directory_with_an_open_mode_is_tightened(db_path: Path) -> None:
    at_version_one(db_path)
    backups = db_path.parent / "db-backups"
    backups.mkdir(mode=0o755)
    backups.chmod(0o755)
    apply(db_path, registry())
    assert mode(backups) == 0o700


def test_no_copy_when_nothing_is_pending(db_path: Path) -> None:
    at_version_one(db_path)
    assert apply(db_path, registry=()).backup is None
    assert not (db_path.parent / "db-backups").exists()


def test_no_copy_of_a_database_that_has_a_schema_and_no_rows(db_path: Path) -> None:
    at_version_one(db_path, rows=False)
    result = apply(db_path, registry())
    assert result.applied == ("add_note", "add_probe", "after_failure")
    assert result.backup is None and not (db_path.parent / "db-backups").exists()


def test_only_the_three_newest_copies_are_kept_and_the_others_are_overwritten_first(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_version_one(db_path)
    backups = db_path.parent / "db-backups"
    backups.mkdir(mode=0o700)
    # v10 sorts before v2 and v9 as text, and was taken after them: only the stamp says so
    old = [
        "app-v9-20240101T000000000000Z.db",
        "app-v10-20240102T000000000000Z.db",
        "app-v2-20240103T000000000000Z.db",
        "app-v1-20240104T000000000000Z.db",
    ]
    for name in old:
        (backups / name).write_bytes(b"an older copy")
    (backups / "notes.txt").write_text("not a copy")
    (backups / "app-v1-latest.db").write_text("not named like one of ours")
    (backups / "app-v3-20240101T000000000000Z.db.d").mkdir()
    (backups / "app-v1-20230101T000000000000Z.db").symlink_to(backups / "notes.txt")
    overwritten: list[tuple[str, bool]] = []
    real = secure_delete.overwrite

    def watch(path: Path) -> None:
        overwritten.append((path.name, path.exists()))
        real(path)

    monkeypatch.setattr(migrations, "overwrite", watch)
    result = apply(db_path, registry())
    assert result.backup is not None
    kept = sorted(p.name for p in backups.iterdir())
    assert kept == sorted(
        [
            result.backup.name,
            old[3],
            old[2],
            "notes.txt",
            "app-v1-latest.db",
            "app-v3-20240101T000000000000Z.db.d",
            "app-v1-20230101T000000000000Z.db",
        ]
    )
    assert overwritten == [(old[0], True), (old[1], True)], "oldest first, each still on disk"
    assert (backups / "notes.txt").read_text() == "not a copy"


def test_an_upgrade_that_cannot_take_its_copy_does_not_start(db_path: Path) -> None:
    at_version_one(db_path)
    (db_path.parent / "db-backups").write_text("a file where the directory should be")
    ran: list[int] = []
    with pytest.raises(MigrationError, match=r"could not back up .*nothing was changed"):
        apply(db_path, registry(ran))
    assert ran == [] and user_version(db_path) == 1
    assert "note" not in columns(db_path, "documents")


def test_a_copy_that_fails_its_integrity_check_is_removed_and_the_upgrade_does_not_start(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_version_one(db_path)
    monkeypatch.setattr(migrations, "_copy_is_sound", lambda _path: False)
    ran: list[int] = []
    with pytest.raises(MigrationError, match="integrity check"):
        apply(db_path, registry(ran))
    assert ran == [] and user_version(db_path) == 1
    assert list((db_path.parent / "db-backups").iterdir()) == []


def test_a_copy_is_sound_only_if_sqlite_says_so(db_path: Path) -> None:
    at_version_one(db_path)
    assert migrations._copy_is_sound(db_path) is True
    unreadable = db_path.parent / "not-a-database.db"
    unreadable.write_bytes(b"x" * 4096)
    assert migrations._copy_is_sound(unreadable) is False


def test_retention_and_the_copies_zero_files_with_the_same_function() -> None:
    from procurement_agent.jobs import retention

    assert retention.overwrite is secure_delete.overwrite is migrations.overwrite


# --- two processes, a crash, a lock ------------------------------------------------------------


def test_two_starts_at_once_run_a_migration_once(db_path: Path) -> None:
    at_version_one(db_path)
    ran: list[int] = []

    def start() -> Any:
        return apply(db_path, registry(ran, pause=0.5)[:1])

    results = concurrently([start, start])
    assert ran == [2], "the second start waited for the lock, looked again, and found it done"
    assert sorted(r.applied for r in results) == [(), ("add_note",)]
    assert user_version(db_path) == 2


def test_two_starts_on_a_new_database_create_it_once(tmp_path: Path) -> None:
    """SQLite answers "locked" at once, not after the busy timeout, to the second of two
    connections that switch the same new file to WAL; that failed about a third of the rounds."""
    expected = {(o["type"], o["name"]) for o in _snapshot(LATEST_VERSION)}
    for n in range(12):
        db_path = tmp_path / f"round{n}" / "app.db"
        db_path.parent.mkdir()
        results = concurrently([lambda p=db_path: apply(p)] * 2)
        assert [r.to_version for r in results] == [LATEST_VERSION] * 2
        assert user_version(db_path) == LATEST_VERSION
        assert {(r[0], r[1]) for r in master(db_path)} == expected


def test_two_starts_on_a_legacy_database_adopt_it_once(db_path: Path) -> None:
    build_legacy(db_path)
    results = concurrently([lambda: apply(db_path, registry=())] * 2)
    assert sorted(r.applied for r in results) == [(), ("baseline",)]
    assert user_version(db_path) == 1


def test_a_database_that_cannot_be_opened_is_reported_at_once_not_waited_for(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no" / "such" / "dir" / "app.db"
    runner = migrations._migration_engine(create_engine(f"sqlite:///{missing}").url, missing, 30)
    started = time.monotonic()
    try:
        with (
            pytest.raises(MigrationError, match="could not open the database") as caught,
            migrations._exclusive(runner, 30),
        ):
            pytest.fail("the block must not run")
    finally:
        runner.dispose()
    assert time.monotonic() - started < 5, "an error that is not 'busy' is not retried"
    assert "OperationalError" in str(caught.value) and str(tmp_path) not in str(caught.value)


_DIES = """
import os, sys
from pathlib import Path
from sqlalchemy import create_engine
from procurement_agent.migrations import Migration, apply_migrations

def dies(conn):
    conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN note VARCHAR(255)")
    conn.exec_driver_sql("CREATE TABLE half_done (x INTEGER)")
    os._exit(3)  # no rollback, no cleanup: the process is gone mid-transaction

path = Path(sys.argv[1])
engine = create_engine(f"sqlite:///{path}")
apply_migrations(engine, path, registry=(Migration(2, "dies", dies),))
"""


def test_a_crash_in_the_middle_of_a_migration_leaves_the_previous_version(
    db_path: Path,
) -> None:
    at_version_one(db_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PA_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [sys.executable, "-c", _DIES, str(db_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert done.returncode == 3, done.stderr[-800:]
    assert user_version(db_path) == 1
    assert "note" not in columns(db_path, "documents") and "half_done" not in tables(db_path)
    assert sql_rows(db_path, "PRAGMA integrity_check") == [("ok",)]
    result = apply(db_path, registry()[:1])  # and the next start simply does it
    assert (result.from_version, result.to_version) == (1, 2)


def test_a_database_another_process_holds_locked_is_reported_not_waited_for_forever(
    db_path: Path,
) -> None:
    at_version_one(db_path)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(MigrationError, match="another process") as caught:
            apply(db_path, registry(), lock_timeout_s=0.2)
        assert "nothing was changed" in str(caught.value)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert user_version(db_path) == 1
    assert apply(db_path, registry()).to_version == 4


# --- init_db -----------------------------------------------------------------------------------


def _audit(path: Path) -> list[tuple[Any, ...]]:
    return sql_rows(
        path, "SELECT actor, action, outcome, detail FROM audit_log WHERE action = 'db.migrate'"
    )


def test_init_db_stamps_a_new_database_and_writes_nothing_to_the_audit_trail(
    db_path: Path,
) -> None:
    dbmod.init_db(db_path).dispose()
    assert user_version(db_path) == LATEST_VERSION
    assert sql_rows(db_path, "SELECT count(*) FROM audit_log") == [(0,)]


def test_init_db_adopts_a_legacy_database_and_records_that_once(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations, "MIGRATIONS", ())
    build_legacy(db_path)
    before = seeded_rows(db_path)
    dbmod.init_db(db_path).dispose()
    assert _audit(db_path) == [("system", "db.migrate", "ok", "v0 -> v1: baseline")]
    assert user_version(db_path) == 1
    for table in ("api_keys", "documents", "jobs"):
        assert seeded_rows(db_path)[table] == before[table]
    dbmod.init_db(db_path).dispose()  # the next start has nothing to record
    assert len(_audit(db_path)) == 1


def test_init_db_records_the_names_of_the_migrations_it_ran_and_nothing_else(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_version_one(db_path)
    monkeypatch.setattr(migrations, "MIGRATIONS", registry()[:2])
    dbmod.init_db(db_path).dispose()
    rows = _audit(db_path)
    assert rows == [("system", "db.migrate", "ok", "v1 -> v3: add_note, add_probe")]
    assert CANARY not in repr(rows) and str(db_path.parent) not in repr(rows)


def test_init_db_refuses_a_newer_database_and_leaves_no_engine_behind(db_path: Path) -> None:
    at_version_one(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 99")
    con.close()
    with pytest.raises(MigrationError, match="newer than this release"):
        dbmod.init_db(db_path)
    assert dbmod._engine is None and dbmod._SessionLocal is None
    assert user_version(db_path) == 99


def test_every_start_still_creates_a_table_nothing_else_is_responsible_for(db_path: Path) -> None:
    """As before versions: a table the models have and the file lacks is created on start."""
    at_version_one(db_path)
    con = sqlite3.connect(db_path)
    con.execute("DROP TABLE check_run_documents")
    con.close()
    dbmod.init_db(db_path).dispose()
    assert "check_run_documents" in tables(db_path)


def test_every_start_puts_back_a_dropped_audit_trigger(db_path: Path) -> None:
    dbmod.init_db(db_path).dispose()
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER audit_log_no_update")
    con.close()
    dbmod.init_db(db_path).dispose()
    assert "audit_log_no_update" in shape(db_path)["triggers"]


def test_the_migration_connection_does_not_change_what_the_application_connection_enforces(
    db_path: Path,
) -> None:
    at_version_one(db_path)
    engine = dbmod.init_db(db_path)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA secure_delete").scalar() == 1
    finally:
        engine.dispose()


# --- looking without touching ------------------------------------------------------------------


def test_a_missing_database_is_reported_and_not_created(db_path: Path) -> None:
    status = inspect_database(db_path)
    assert status.state == "new" and status.version == 0 and status.latest == LATEST_VERSION
    assert status.pending == () and not db_path.exists() and not db_path.parent.exists()


def test_an_empty_file_is_a_new_database_too(db_path: Path) -> None:
    db_path.parent.mkdir()
    db_path.write_bytes(b"")
    status = inspect_database(db_path)
    assert (status.state, status.version, status.pending) == ("new", 0, ())


def test_the_states_a_database_can_be_in(db_path: Path) -> None:
    build_legacy(db_path)
    legacy = inspect_database(db_path, ())
    assert (legacy.state, legacy.version, legacy.pending) == ("legacy", 0, ("baseline",))
    legacy_later = inspect_database(db_path, registry())
    assert legacy_later.pending == ("baseline", "add_note", "add_probe", "after_failure")
    apply(db_path, registry=())
    current = inspect_database(db_path, ())
    assert (current.state, current.version, current.pending) == ("current", 1, ())
    behind = inspect_database(db_path, registry())
    assert (behind.state, behind.latest, behind.pending) == (
        "pending",
        4,
        ("add_note", "add_probe", "after_failure"),
    )
    apply(db_path, registry()[:2])  # a newer release has been here
    ahead = inspect_database(db_path, ())
    assert (ahead.state, ahead.version, ahead.latest, ahead.pending) == ("newer", 3, 1, ())
    assert ahead.problem is not None and "newer than this release" in ahead.problem


def test_inspecting_never_writes_to_the_database(db_path: Path) -> None:
    build_legacy(db_path)
    before = db_path.read_bytes()
    for reg in ((), registry()):
        inspect_database(db_path, reg)
    assert db_path.read_bytes() == before and user_version(db_path) == 0


def test_a_file_that_is_not_a_database_and_a_database_of_another_shape_are_damaged(
    db_path: Path,
) -> None:
    db_path.parent.mkdir()
    db_path.write_bytes(b"this is not a SQLite file, it is " + CANARY.encode() * 20)
    garbage = inspect_database(db_path)
    assert garbage.state == "damaged" and garbage.problem is not None
    assert CANARY not in garbage.problem
    db_path.unlink()
    at_version_one(db_path)
    con = sqlite3.connect(db_path)
    con.execute("ALTER TABLE jobs DROP COLUMN message")
    con.close()
    cut = inspect_database(db_path, ())
    assert cut.state == "damaged" and cut.problem is not None
    assert "jobs has no column message" in cut.problem


# --- the command -------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    yield tmp_path / "data" / "app.db"
    get_settings.cache_clear()


def test_db_status_with_no_database_creates_nothing_and_succeeds(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["db-status"]) == 0
    out = capsys.readouterr().out
    assert "no database yet" in out and f"schema version {LATEST_VERSION}" in out
    assert not cli_env.exists() and not cli_env.parent.exists()


def test_db_status_of_a_current_database(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dbmod.init_db(cli_env).dispose()
    assert cli.main(["db-status"]) == 0
    out = capsys.readouterr().out
    assert f"schema version: {LATEST_VERSION}" in out
    assert f"this release:   {LATEST_VERSION}" in out
    assert "pending:        none" in out and "up to date" in out


def test_db_status_lists_what_the_next_start_would_run_and_runs_none_of_it(
    cli_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    at_version_one(cli_env)
    before = cli_env.read_bytes()
    monkeypatch.setattr(migrations, "MIGRATIONS", registry())
    assert cli.main(["db-status"]) == 0
    out = capsys.readouterr().out
    assert "schema version: 1" in out and "this release:   4" in out
    assert "pending:        add_note, add_probe, after_failure" in out
    assert "next start" in out
    assert cli_env.read_bytes() == before and user_version(cli_env) == 1


def test_db_status_of_a_database_from_before_versions(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_legacy(cli_env)
    assert cli.main(["db-status"]) == 0
    out = capsys.readouterr().out
    assert "schema version: 0" in out and "baseline" in out and "before schema versions" in out
    assert user_version(cli_env) == 0


def test_db_status_of_a_database_newer_than_the_code_fails(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    at_version_one(cli_env)
    con = sqlite3.connect(cli_env)
    con.execute("PRAGMA user_version = 12")
    con.close()
    assert cli.main(["db-status"]) == 1
    captured = capsys.readouterr()
    assert "schema version: 12" in captured.out
    assert "newer than this release" in captured.err


@pytest.mark.parametrize("how", ["garbage", "column"])
def test_db_status_of_a_damaged_database_fails_without_quoting_it(
    cli_env: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    if how == "garbage":
        cli_env.parent.mkdir(parents=True)
        cli_env.write_bytes(CANARY.encode() * 50)
    else:
        at_version_one(cli_env)
        con = sqlite3.connect(cli_env)
        con.execute("ALTER TABLE documents DROP COLUMN error")
        con.close()
    assert cli.main(["db-status"]) == 1
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    assert str(cli_env.parent) not in captured.err


def test_db_status_prints_no_row_of_the_database(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_legacy(cli_env)
    assert cli.main(["db-status"]) == 0
    shown = capsys.readouterr()
    assert CANARY not in shown.out + shown.err and "alice" not in shown.out


def test_other_commands_report_a_newer_database_instead_of_a_traceback(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    at_version_one(cli_env)
    con = sqlite3.connect(cli_env)
    con.execute("PRAGMA user_version = 12")
    con.close()
    assert cli.main(["list-keys"]) == 1
    assert "newer than this release" in capsys.readouterr().err


def test_a_command_that_opens_the_database_upgrades_it_and_the_trail_says_so(
    cli_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations, "MIGRATIONS", ())
    build_legacy(cli_env)
    assert cli.main(["list-keys"]) == 0
    assert _audit(cli_env) == [("system", "db.migrate", "ok", "v0 -> v1: baseline")]
    assert "alice" in capsys.readouterr().out  # the seeded key, listed by the command itself


def test_the_module_entry_point_prints_the_status(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PA_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PA_DATA_DIR"] = str(tmp_path / "data")
    env["PA_OLLAMA_BASE_URL"] = "http://127.0.0.1:1"
    done = subprocess.run(
        [sys.executable, "-m", "procurement_agent.cli", "db-status"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert "no database yet" in done.stdout
    assert not (tmp_path / "data").exists()

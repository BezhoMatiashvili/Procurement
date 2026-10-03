"""A versioned schema: what a start does to a database an earlier release wrote.

``Base.metadata.create_all`` makes a table that does not exist and never changes one that does, so a
release that alters a table needs its own upgrade step. The database carries its schema version in
SQLite's ``PRAGMA user_version`` (no table of its own), and a start meets one of five cases:

* **new** (no tables): created from the models, audit triggers included, and stamped with the
  latest version - none of the migrations below applies to a schema that was just built current;
* **version 0**, tables but no version (written before this module existed): checked against
  ``BASELINE_SCHEMA``, the shape of version 1, then adopted as version 1. A table or column that is
  not as expected is a refusal that names it, never a guess and never an ``ALTER``;
* **behind**: the pending ``MIGRATIONS`` run in order, each in a transaction of its own that also
  writes the new version, so a failure leaves the database at the last good version;
* **ahead** of this release: refused, so a downgrade cannot damage what a newer one wrote;
* **equal**: nothing to do.

Before the first migration of a database that holds rows, a consistent copy is taken with SQLite's
online backup API into ``<data dir>/db-backups/`` (owner-only). The copy and the first migration
happen under one write lock, so the copy is of the version it is named for even with two processes
starting.

**A copy keeps no text a person typed.** It is read into memory and blanked there, and only the
blanked database is written (``VACUUM INTO``): the unblanked bytes never reach the backup
directory, so a process killed half-way leaves nothing behind that retention would have removed.
``SCRUBBED_COLUMNS`` is the list - ``documents.display_name`` (the file name), ``documents.sha256``
(a hash of the document), ``documents.error``, ``jobs.params`` (the question and the options typed),
``jobs.message`` and ``check_runs.params`` (the options of a check) - and what is left (key hashes,
ids, statuses, times, sizes, the audit trail) is bounded by ``PA_DB_BACKUP_KEEP_DAYS``:
``expire_backups``, called by the retention sweep, overwrites and removes a copy older than that.
A copy is of the schema *before* the migrations, so the list never loses an entry (a column a
migration renames keeps its old name here), and a column that will hold text a person typed, a file
name or a value derived from a document's content goes on it; ``tests/unit/test_migrations.py``
fails until every column of every table is either on it or on that test's list of kept ones.

A copy is kept only if the upgrade succeeded, and then the newest three; a start that changed
nothing before failing removes the copy it took (it is the file as it stands), so a start that is
retried does not wear out the copy from before the upgrade, which is the one to go back to.

Adding a migration is a function and a line - and the model change it describes::

    def _add_note(conn: Connection) -> None:
        conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN note VARCHAR(255)")

    MIGRATIONS = (Migration(2, "document_note", _add_note),)

A migration gets the connection of its transaction and must not commit, roll back or begin one.
Foreign keys are off on that connection (SQLite ignores the pragma inside a transaction), and
``PRAGMA foreign_key_check`` must come back empty or the step is undone. ``BASELINE_SCHEMA`` stays
as it is: it is the version-1 shape a version-0 file is held to, whatever later versions add.
Record the new schema with ``scripts/freeze_schema.py`` (``tests/fixtures/schema_v2.json``); the
tests compare a new database with it and an upgraded one with a new one.

Nothing here reads the content of a row. Messages, log fields and the audit detail carry
versions, migration names and the names of *our* tables and columns - what the file holds beyond
that is a count.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import column, create_engine, event, literal_column, select, table, update
from sqlalchemy.engine import URL, Connection, Engine, RootTransaction
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.pool import NullPool

from procurement_agent import models  # noqa: F401  (register tables)
from procurement_agent.db import Base, configure_connection, create_audit_triggers
from procurement_agent.security.audit import audit_now, log
from procurement_agent.security.safe_errors import ContentFree
from procurement_agent.security.secure_delete import overwrite

__all__ = [
    "BASELINE_SCHEMA",
    "LATEST_VERSION",
    "MIGRATIONS",
    "SCRUBBED_COLUMNS",
    "DatabaseStatus",
    "Migration",
    "MigrationError",
    "MigrationResult",
    "apply_migrations",
    "check_registry",
    "columns_of",
    "expire_backups",
    "inspect_database",
    "latest_version",
    "record_migration",
]

#: The schema ``create_all`` built when versions began. Every later version is a migration away.
BASELINE_VERSION = 1
#: What the step that adopts a version-0 database is called in results, logs and the audit trail.
BASELINE_NAME = "baseline"
BACKUP_DIR_NAME = "db-backups"
KEEP_BACKUPS = 3
#: How long a start waits for another process that is upgrading the same file.
LOCK_TIMEOUT_S = 30.0

#: Version 1, frozen: the tables and columns of the schema when versions began. A version-0 file is
#: checked against this and not against the models, which move on with every migration (a column a
#: later migration adds is not in a file that has not met it). Do not edit it to follow a model.
BASELINE_SCHEMA: Mapping[str, tuple[str, ...]] = {
    "api_keys": ("id", "user", "key_hash", "prefix", "created_at", "revoked_at", "last_used_at"),
    "audit_log": (
        "id",
        "at",
        "actor",
        "action",
        "document_id",
        "job_id",
        "ip",
        "outcome",
        "detail",
    ),
    "check_run_documents": ("run_id", "document_id", "role"),
    "check_runs": ("id", "owner", "kind", "key", "params", "created_at", "last_opened_at"),
    "documents": (
        "id",
        "owner",
        "display_name",
        "kind",
        "size_bytes",
        "sha256",
        "page_count",
        "status",
        "error",
        "created_at",
        "parsed_at",
        "expires_at",
    ),
    "jobs": (
        "id",
        "document_id",
        "owner",
        "kind",
        "params",
        "status",
        "progress",
        "message",
        "created_at",
        "started_at",
        "finished_at",
    ),
    "ui_sessions": (
        "id",
        "token_hash",
        "api_key_id",
        "user",
        "created_at",
        "expires_at",
        "revoked_at",
        "last_seen_at",
    ),
}

#: What a pre-upgrade copy has blanked: table -> column -> what it is set to. The columns that hold
#: a file name, a question or an option a person typed, or a value derived from a document's
#: content, plus the free-text ones whose contents are only known to be harmless today. Never
#: remove an entry - see the module docstring.
SCRUBBED_NAME = "[scrubbed]"
SCRUBBED_COLUMNS: Mapping[str, Mapping[str, str | None]] = {
    "documents": {"display_name": SCRUBBED_NAME, "sha256": "0" * 64, "error": None},
    "jobs": {"params": "{}", "message": None},
    "check_runs": {"params": "{}"},
}
#: The time in a copy's name: UTC, with microseconds so that two starts never choose one name.
_STAMP = "%Y%m%dT%H%M%S%fZ"
#: A half-written copy older than this is debris from a crash.
_STALE_PARTIAL_S = 3600

#: A migration's name goes into the audit trail and the logs: an identifier, never free text.
_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_LOCKED = "the database is locked by another process (is it being upgraded?); nothing was changed"
#: How long to wait before asking SQLite again when it said "busy" without waiting.
_RETRY_S = 0.05


class MigrationError(ContentFree, RuntimeError):
    """The database cannot be brought to this release's version, and was left as it was (or, for a
    migration that failed after others succeeded, at the last version that did)."""


@dataclass(frozen=True)
class Migration:
    """One step from ``version - 1`` to ``version``: DDL and data work on the transaction's
    connection. ``name`` is a short identifier (it is audited)."""

    version: int
    name: str
    apply: Callable[[Connection], None]


#: The steps after the baseline, in order, versions 2, 3, ... with no gaps.
MIGRATIONS: tuple[Migration, ...] = ()


def check_registry(registry: Sequence[Migration]) -> None:
    """Raise ``ValueError`` unless the versions run 2, 3, ... and every name is a new identifier."""
    seen = {BASELINE_NAME}
    for expected, m in enumerate(registry, start=BASELINE_VERSION + 1):
        if m.version != expected:
            msg = f"migration registry: version {expected} is expected, found {m.version}"
            raise ValueError(msg)
        if not _NAME.fullmatch(m.name) or m.name in seen:
            msg = (
                f"migration registry: version {m.version} needs a name of its own - lower-case "
                f"letters, digits and underscores, at most 40, got {m.name!r}"
            )
            raise ValueError(msg)
        seen.add(m.name)


def latest_version(registry: Sequence[Migration] | None = None) -> int:
    chosen = MIGRATIONS if registry is None else registry
    return chosen[-1].version if chosen else BASELINE_VERSION


check_registry(MIGRATIONS)
LATEST_VERSION: int = latest_version()


@dataclass(frozen=True)
class MigrationResult:
    """What a start did. ``applied`` is empty for a new database and for one already current."""

    from_version: int
    to_version: int
    applied: tuple[str, ...]
    backup: Path | None


@dataclass(frozen=True)
class DatabaseStatus:
    """What ``inspect_database`` saw. ``state`` is one of ``new``, ``legacy`` (version 0, to be
    adopted), ``current``, ``pending``, ``newer`` (than this release) and ``damaged``; ``problem``
    is set for the last two. ``version`` is None when the file could not be read."""

    version: int | None
    latest: int
    state: str
    pending: tuple[str, ...]
    problem: str | None = None


# --- reading a database ------------------------------------------------------------------------


def _version(conn: Connection) -> int:
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())


def _stored_version(engine: Engine) -> int:
    with engine.connect() as probe:
        return _version(probe)


def _has_schema(conn: Connection) -> bool:
    sql = "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    return int(conn.exec_driver_sql(sql).scalar_one()) > 0


def columns_of(conn: Connection, table_name: str) -> list[str]:
    """The column names of a table, for a migration that has to ask before it alters."""
    rows = conn.exec_driver_sql("SELECT name FROM pragma_table_info(?)", (table_name,))
    return [str(r[0]) for r in rows]


def _holds_data(conn: Connection) -> bool:
    """Whether any table has a row: a schema with nothing in it is not worth a copy."""
    sql = (
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    )
    for (name,) in conn.exec_driver_sql(sql).all():
        first: Any = conn.execute(
            select(literal_column("1")).select_from(table(name)).limit(1)
        ).first()
        if first is not None:
            return True
    return False


def _schema_problem(conn: Connection, expected: Mapping[str, Sequence[str]]) -> str | None:
    """The first way the tables that exist differ from ``expected``, in our words, or None.

    A table that is not there is not a problem (it is created); one that is must have exactly the
    expected columns. What the file has beyond them is counted, not named: it came from the file.
    """
    kinds = {
        r[0]: r[1]
        for r in conn.exec_driver_sql(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    for name, wanted in expected.items():
        kind = kinds.get(name)
        if kind is None:
            continue
        if kind != "table":
            return f"{name} exists but is not a table"
        have = columns_of(conn, name)
        for wanted_column in wanted:
            if wanted_column not in have:
                return f"table {name} has no column {wanted_column}"
        extra = len(set(have) - set(wanted))
        if extra:
            return f"table {name} has {extra} column(s) this release does not know"
    return None


def _live_schema() -> dict[str, tuple[str, ...]]:
    return {t.name: tuple(c.name for c in t.columns) for t in Base.metadata.sorted_tables}


def _refuse_newer(found: int, latest: int) -> None:
    if found > latest:
        msg = (
            f"database schema version {found} is newer than this release ({latest}); "
            "not started, so a downgrade cannot damage it"
        )
        raise MigrationError(msg)


# --- the copy taken before an upgrade ----------------------------------------------------------


def _read_only_uri(db_path: Path) -> str:
    """The file as a read-only SQLite URI (``as_uri`` wants it absolute and escapes it)."""
    return f"{db_path.resolve().as_uri()}?mode=ro"


def _backup_directory(db_path: Path) -> Path:
    return db_path.parent / BACKUP_DIR_NAME


def _copy_is_sound(path: Path) -> bool:
    try:
        con = sqlite3.connect(path)
        try:
            return con.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def _scrub(scratch: sqlite3.Connection) -> None:
    """Blank ``SCRUBBED_COLUMNS`` in the in-memory copy - those of them the database has: a copy
    of an old file may lack a table, and a damaged one a column, and neither stops the copy."""
    present = {r[0] for r in scratch.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for name, assignments in SCRUBBED_COLUMNS.items():
        if name not in present:
            continue
        have = {r[0] for r in scratch.execute("SELECT name FROM pragma_table_info(?)", (name,))}
        wanted = {c: v for c, v in assignments.items() if c in have}
        if not wanted:
            continue
        # ``UPDATE documents SET display_name=:display_name, ...``: the bind names are the columns'
        statement = update(table(name, *(column(c) for c in wanted))).values(wanted)
        scratch.execute(str(statement), wanted)


def _write_scrubbed_copy(source: Path, target: Path) -> None:
    """A consistent snapshot of the live database (SQLite's online backup API) read into memory,
    blanked there, and written compact: the file holds only what is left."""
    with (
        contextlib.closing(sqlite3.connect(_read_only_uri(source), uri=True)) as live,
        contextlib.closing(sqlite3.connect(":memory:")) as scratch,
    ):
        scratch.execute("PRAGMA temp_store=MEMORY")
        live.backup(scratch)
        _scrub(scratch)
        scratch.commit()
        scratch.execute("VACUUM INTO ?", (str(target),))


def _take_backup(db_path: Path, from_version: int) -> Path:
    """The scrubbed copy, written owner-only under a name that carries the version and the time.
    It is checked before it is trusted, and nothing is left behind if it cannot be made."""
    directory = _backup_directory(db_path)
    stamp = datetime.now(UTC).strftime(_STAMP)
    final = directory / f"{db_path.stem}-v{from_version}-{stamp}.db"
    partial = directory / f".{final.name}.partial"
    done = False
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)  # a directory that already existed keeps its old mode
        os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        _write_scrubbed_copy(db_path, partial)
        if not _copy_is_sound(partial):
            msg = (
                "the pre-upgrade copy of the database failed its integrity check; "
                "nothing was changed"
            )
            raise MigrationError(msg)
        partial.replace(final)
        done = True
        return final
    except (OSError, sqlite3.Error, MemoryError) as e:
        error_kind = type(e).__name__
        msg = (
            f"could not back up the database before upgrading it ({error_kind}); "
            "nothing was changed"
        )
        raise MigrationError(msg) from e
    finally:
        if not done:
            for suffix in ("", "-journal", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(f"{partial}{suffix}").unlink()


def _our_copies(db_path: Path) -> list[tuple[datetime, Path]]:
    """The copies of this database in its backup directory, oldest first, by the time in the name
    (``v9`` sorts after ``v10`` as text). Only a regular file named exactly as this module names
    its copies counts; anything else in the directory is not ours to touch."""
    ours = re.compile(rf"{re.escape(db_path.stem)}-v\d+-(?P<stamp>\d{{8}}T\d{{12}}Z)\.db")
    found: list[tuple[datetime, Path]] = []
    for path in _backup_directory(db_path).iterdir():
        match = ours.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            with contextlib.suppress(ValueError):  # digits that are not a date
                found.append((datetime.strptime(match["stamp"], _STAMP).replace(tzinfo=UTC), path))
    return sorted(found)


def _remove_copy(path: Path) -> None:
    overwrite(path)
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _prune_backups(db_path: Path) -> None:
    """Keep the newest ``KEEP_BACKUPS`` copies; overwrite the rest, then unlink them."""
    with contextlib.suppress(OSError):
        for _, path in _our_copies(db_path)[:-KEEP_BACKUPS]:
            _remove_copy(path)


def expire_backups(db_path: Path, keep_days: int) -> int:
    """Remove the copies taken more than ``keep_days`` days ago, and half-written ones a crash left;
    returns how many files went. Called by the retention sweep, so what a copy still holds after
    blanking - key hashes, ids, the audit trail - lives at most ``keep_days`` plus a sweep interval.
    ``keep_days=0`` removes every copy: the first sweep after an upgrade takes the one it made."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=keep_days)
    removed = 0
    with contextlib.suppress(OSError):  # no directory, no copies
        for taken, path in _our_copies(db_path):
            if taken < cutoff:
                _remove_copy(path)
                removed += 1
        debris = re.compile(rf"\.{re.escape(db_path.stem)}-v\d+-\d{{8}}T\d{{12}}Z\.db\.partial")
        for path in _backup_directory(db_path).iterdir():
            if not (debris.fullmatch(path.name) and path.is_file() and not path.is_symlink()):
                continue  # not ours; and nothing is statted before it is known to be
            if now.timestamp() - path.stat().st_mtime > _STALE_PARTIAL_S:
                _remove_copy(path)
                removed += 1
    return removed


def _copy_first(conn: Connection, db_path: Path, version: int) -> Path | None:
    """Called under the write lock, before the first step that changes anything."""
    return _take_backup(db_path, version) if _holds_data(conn) else None


# --- applying ----------------------------------------------------------------------------------


def _migration_engine(url: URL, db_path: Path, lock_timeout_s: float) -> Engine:
    """A short-lived engine for the upgrade, on the same file as the application's.

    The driver is left in autocommit and each transaction starts with ``BEGIN IMMEDIATE`` (the
    recipe in SQLAlchemy's pysqlite notes): the write lock is taken first, so what a step reads
    cannot change before it commits, and DDL is rolled back with the rest. Foreign keys are off.
    """
    runner = create_engine(url, poolclass=NullPool, connect_args={"timeout": lock_timeout_s})

    @event.listens_for(runner, "connect")
    def _connect(dbapi_conn: Any, _: object) -> None:
        dbapi_conn.isolation_level = None
        configure_connection(dbapi_conn, db_path, foreign_keys=False)

    @event.listens_for(runner, "begin")
    def _begin_immediate(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return runner


def _is_busy(e: OperationalError) -> bool:
    code = getattr(e.orig, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _patiently[T](attempt: Callable[[], T], timeout_s: float) -> T:
    """Run ``attempt`` again for as long as SQLite says "busy", up to ``timeout_s``.

    The busy timeout covers a lock that is being waited for; it does not cover everything. Two
    processes that create the same new file both switch it to WAL, and the second is told
    "locked" at once - which is the answer to ask again, not to give up on.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return attempt()
        except OperationalError as e:
            if not _is_busy(e):
                error_kind = type(e).__name__
                msg = f"could not open the database ({error_kind}); nothing was changed"
                raise MigrationError(msg) from e
            if time.monotonic() >= deadline:
                raise MigrationError(_LOCKED) from e
            time.sleep(_RETRY_S)


def _begin(runner: Engine) -> tuple[Connection, RootTransaction]:
    conn = runner.connect()
    try:
        return conn, conn.begin()
    except BaseException:
        conn.close()
        raise


@contextmanager
def _exclusive(runner: Engine, timeout_s: float) -> Iterator[Connection]:
    """One transaction under the write lock: committed when the block ends, undone on a raise."""
    conn, transaction = _patiently(lambda: _begin(runner), timeout_s)
    with conn:
        try:
            yield conn
            transaction.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                transaction.rollback()
            raise


def _stamp(conn: Connection, version: int) -> None:
    conn.exec_driver_sql(f"PRAGMA user_version = {int(version)}")


def _create_current(conn: Connection, latest: int) -> None:
    Base.metadata.create_all(conn)
    create_audit_triggers(conn)
    _stamp(conn, latest)


def _check_baseline(conn: Connection) -> None:
    """The tables a version-0 file has must be the baseline's. Before anything is copied or made,
    so a refusal has changed nothing and left nothing behind."""
    problem = _schema_problem(conn, BASELINE_SCHEMA)
    if problem is not None:
        msg = f"the database does not match this release: {problem}; left unchanged"
        raise MigrationError(msg)


def _adopt_baseline(conn: Connection) -> None:
    """Version 0 becomes version 1: the baseline's tables the file lacks are made."""
    wanted = [t for t in Base.metadata.sorted_tables if t.name in BASELINE_SCHEMA]
    Base.metadata.create_all(conn, tables=wanted)
    _stamp(conn, BASELINE_VERSION)


def _run_migration(conn: Connection, m: Migration, before: int) -> None:
    version, step = m.version, m.name
    try:
        m.apply(conn)
        broken = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    except Exception as e:
        error_kind = type(e).__name__
        msg = (
            f"migration {version} ({step}) failed with {error_kind} and was rolled back; "
            f"the database stays at version {before}"
        )
        raise MigrationError(msg) from e
    if broken:
        count = len(broken)
        msg = (
            f"migration {version} ({step}) left {count} row(s) that break a foreign key; "
            "rolled back"
        )
        raise MigrationError(msg)
    _stamp(conn, version)


def _upgrade(
    runner: Engine,
    db_path: Path,
    registry: Sequence[Migration],
    latest: int,
    start: int,
    timeout_s: float,
) -> MigrationResult:
    applied: list[str] = []
    backup: Path | None = None
    copy_settled = False  # whether the question of a copy has been put, and answered, this run
    try:
        if start == 0:
            with _exclusive(runner, timeout_s) as conn:
                version = _version(conn)
                _refuse_newer(version, latest)
                if version == 0 and not _has_schema(conn):
                    _create_current(conn, latest)
                    return MigrationResult(0, latest, (), None)
                if version == 0:
                    _check_baseline(conn)
                    if latest > BASELINE_VERSION:  # a migration will follow: copy what it changes
                        backup, copy_settled = _copy_first(conn, db_path, 0), True
                    _adopt_baseline(conn)
                    applied.append(BASELINE_NAME)
            if applied:
                log.info("db_migration_applied", version=BASELINE_VERSION, migration=BASELINE_NAME)
        for m in registry:
            with _exclusive(runner, timeout_s) as conn:
                current = _version(conn)  # again, under the lock: another process may have run
                _refuse_newer(current, latest)
                if current >= m.version:
                    continue
                if not copy_settled:
                    backup, copy_settled = _copy_first(conn, db_path, current), True
                _run_migration(conn, m, current)
                applied.append(m.name)
            log.info("db_migration_applied", version=m.version, migration=m.name)
    except BaseException:
        if backup is not None and not applied:
            _remove_copy(backup)  # nothing changed, so the copy is the file as it stands
        raise
    if backup is not None:
        _prune_backups(db_path)  # only now: a failing start must not wear out the copies
    return MigrationResult(start, latest, tuple(applied), backup)


def apply_migrations(
    engine: Engine,
    db_path: Path,
    *,
    registry: Sequence[Migration] | None = None,
    lock_timeout_s: float = LOCK_TIMEOUT_S,
) -> MigrationResult:
    """Bring the database ``engine`` points at to the latest version; see the module docstring.

    ``registry`` is for tests (migrations of their own); the application passes nothing and gets
    ``MIGRATIONS``. A start that has nothing to do reads one number and takes no lock.
    """
    chosen = tuple(MIGRATIONS if registry is None else registry)
    check_registry(chosen)
    latest = latest_version(chosen)
    found = _patiently(lambda: _stored_version(engine), lock_timeout_s)
    _refuse_newer(found, latest)
    if found == latest:
        return MigrationResult(found, found, (), None)
    runner = _migration_engine(engine.url, db_path, lock_timeout_s)
    try:
        return _upgrade(runner, db_path, chosen, latest, found, lock_timeout_s)
    finally:
        runner.dispose()


def record_migration(result: MigrationResult) -> None:
    """One audit row for an upgrade that ran: versions and migration names, nothing else."""
    detail = f"v{result.from_version} -> v{result.to_version}: {', '.join(result.applied)}"
    audit_now(actor="system", action="db.migrate", detail=detail)


# --- looking without touching ------------------------------------------------------------------


def inspect_database(db_path: Path, registry: Sequence[Migration] | None = None) -> DatabaseStatus:
    """Where a database stands against this release, from a read-only connection.

    Never creates a file, never changes one: ``cli db-status`` is for looking before an upgrade.
    A file that cannot be read as SQLite, or whose tables are not the ones this release expects
    (at the version where that can be told), is ``damaged``.
    """
    chosen = tuple(MIGRATIONS if registry is None else registry)
    check_registry(chosen)
    latest = latest_version(chosen)
    if not db_path.exists():
        return DatabaseStatus(0, latest, "new", ())
    reader = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(_read_only_uri(db_path), uri=True),
        poolclass=NullPool,
    )
    try:
        with reader.connect() as conn:
            version = _version(conn)
            has_schema = _has_schema(conn)
            names = tuple(m.name for m in chosen if m.version > max(version, BASELINE_VERSION))
            if version > latest:
                message = (
                    f"database schema version {version} is newer than this release ({latest}); "
                    "it will not be started on it"
                )
                return DatabaseStatus(version, latest, "newer", (), message)
            if version == 0 and not has_schema:
                return DatabaseStatus(0, latest, "new", ())
            if version == 0:
                state, pending, expected = "legacy", (BASELINE_NAME, *names), BASELINE_SCHEMA
            elif version < latest:
                state, pending, expected = "pending", names, {}
            else:
                state, pending, expected = "current", (), _live_schema()
            problem = _schema_problem(conn, expected)
            if problem is not None:
                return DatabaseStatus(version, latest, "damaged", pending, problem)
            return DatabaseStatus(version, latest, state, pending)
    except DatabaseError:
        return DatabaseStatus(
            None, latest, "damaged", (), "the file cannot be read as a SQLite database"
        )
    finally:
        reader.dispose()

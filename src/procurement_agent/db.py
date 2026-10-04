"""SQLAlchemy engine/session for the local SQLite database."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


#: The application's own log (``security.audit`` configures it; importing that here is circular).
_log = structlog.get_logger("procurement_agent")

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


#: The audit trail is append-only. The application only ever inserts into it; a trigger makes SQLite
#: refuse anything else, so a bug, a stray ``DELETE`` or an injection through the ORM cannot rewrite
#: history. Someone with write access to the file can drop the triggers - this guards against
#: mistakes and application-level tampering, not against the owner of the disk - which is why the
#: one deliberate way to shorten the trail (``prune_audit``) does it inside a transaction that puts
#: them back, and the file itself is owner-only.
_AUDIT_TRIGGERS = {
    "audit_log_no_update": (
        "CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    ),
    "audit_log_no_delete": (
        "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    ),
}


def create_audit_triggers(conn: Connection) -> None:
    """The append-only triggers, on ``conn`` and inside whatever transaction it is in."""
    for ddl in _AUDIT_TRIGGERS.values():
        conn.exec_driver_sql(ddl)


def install_audit_triggers(engine: Engine) -> None:
    with engine.begin() as conn:
        create_audit_triggers(conn)


def prune_audit(older_than: datetime) -> int:
    """Delete audit rows from before ``older_than``; the one sanctioned way to shorten the trail.

    The delete trigger is dropped, the rows deleted and the trigger recreated inside ONE
    transaction, begun explicitly: Python's ``sqlite3`` only opens a transaction before an
    INSERT/UPDATE/DELETE, so a bare ``DROP TRIGGER`` ahead of the delete would be committed on its
    own - and a failure afterwards would leave the trail without its protection. SQLite's DDL is
    transactional, so a failure anywhere rolls the drop back with the rest.
    """
    raw = get_engine().raw_connection()
    connection: Any = raw.driver_connection  # the sqlite3 connection itself
    previous = connection.isolation_level
    # SQLAlchemy stores a DateTime as this text, and the raw connection has no adapter for it
    cutoff = older_than.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    try:
        connection.isolation_level = None  # statements run as written; we say BEGIN and COMMIT
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
            removed = connection.execute("DELETE FROM audit_log WHERE at < ?", (cutoff,)).rowcount
            connection.execute(_AUDIT_TRIGGERS["audit_log_no_delete"])
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return int(removed)
    finally:
        connection.isolation_level = previous
        raw.close()


def secure_database_files(db_path: Path) -> None:
    """Owner-only on the database and the files SQLite keeps beside it.

    ``app.db-wal`` and ``app.db-shm`` hold recently written pages - the same content as the
    database - and SQLite creates them with the process's umask, after ``init_db`` has already
    tightened the main file. Best effort: a file that does not exist yet is skipped.
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        with contextlib.suppress(OSError):
            Path(f"{db_path}{suffix}").chmod(0o600)


def checkpoint(truncate: bool = True) -> None:
    """Write the write-ahead log back into the database and empty it.

    Deleted rows (a retention sweep, a scrubbed job) would otherwise stay readable in the WAL file
    until SQLite next recycles it, which on a quiet server can be a long time. A checkpoint that
    fails, or that a reader holding the log kept from finishing, leaves them there: it never
    stops the caller, but it is said in the log (the kind of failure, never its message).
    """
    if _engine is None:
        return
    try:
        with _engine.connect() as conn:
            row = conn.exec_driver_sql(
                f"PRAGMA wal_checkpoint({'TRUNCATE' if truncate else 'PASSIVE'})"
            ).fetchone()
    except Exception as e:
        _log.warning("checkpoint_failed", error=type(e).__name__)
        return
    if row is not None and row[0]:  # busy: the log could not be emptied, a reader holds it
        _log.warning("checkpoint_busy", log_pages=row[1], written_back=row[2])


def configure_connection(dbapi_conn: Any, db_path: Path, *, foreign_keys: bool = True) -> None:
    """What every connection to the database runs with, and the file modes that follow.

    ``foreign_keys`` is off only for the connection a migration runs on (``migrations``): the
    pragma is ignored inside a transaction, and with it on, rebuilding a table - drop the old one,
    rename the new - would delete every row that references it.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    cur.execute("PRAGMA synchronous=NORMAL")
    # A deleted value is overwritten with zeros instead of left in the page: a retention sweep
    # that removes a row should not leave its text in the file for a later read.
    cur.execute("PRAGMA secure_delete=ON")
    # A database file is data, not a program: no function or view it defines may run.
    cur.execute("PRAGMA trusted_schema=OFF")
    cur.execute("PRAGMA cell_size_check=ON")
    cur.close()
    secure_database_files(db_path)


def init_db(db_path: Path) -> Engine:
    """Create the engine, bring the schema to this release's version, lock down file perms.

    The schema is ``migrations.apply_migrations``' business: a new file is created at the latest
    version, one an earlier release wrote is adopted or upgraded (a copy first), one a later
    release wrote is refused. ``create_all`` and the audit triggers still run on every start,
    after that, for whatever no migration is responsible for.
    """
    global _engine, _SessionLocal
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        db_path.parent.chmod(0o700)  # a directory that already existed keeps its old mode
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: object, _: object) -> None:
        configure_connection(dbapi_conn, db_path)

    from procurement_agent import migrations, models  # noqa: F401  (register tables)

    try:
        result = migrations.apply_migrations(engine, db_path)
        Base.metadata.create_all(engine)
        install_audit_triggers(engine)
    except BaseException:
        engine.dispose()  # a refused start leaves no open file behind
        raise
    secure_database_files(db_path)
    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    if result.applied:
        migrations.record_migration(result)  # the audit trail needs the session factory above
    return engine


def get_engine() -> Engine:
    if _engine is None:
        msg = "init_db() has not been called"
        raise RuntimeError(msg)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        msg = "init_db() has not been called"
        raise RuntimeError(msg)
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s

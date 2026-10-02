"""File modes, secure deletion and core dumps: the ways data outlives its use on the disk."""

from __future__ import annotations

import os
import resource
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import checkpoint, init_db, secure_database_files, session_scope
from procurement_agent.main import create_app
from procurement_agent.models import Document
from procurement_agent.security.hardening import harden_process

KEY = "pa_devkey_for_tests_only"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def current_umask() -> int:
    previous = os.umask(0)
    os.umask(previous)
    return previous


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    s = Settings(env="dev", data_dir=tmp_path / "data", ollama_base_url="http://127.0.0.1:1")
    yield s
    dbmod._engine = None
    dbmod._SessionLocal = None


# --- the process -------------------------------------------------------------------------------


def test_harden_process_sets_the_umask_and_the_core_limit_and_puts_them_back() -> None:
    before_umask = current_umask()
    before_core = resource.getrlimit(resource.RLIMIT_CORE)
    restore = harden_process()
    try:
        assert current_umask() == 0o077
        assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
    finally:
        restore()
    assert current_umask() == before_umask
    assert resource.getrlimit(resource.RLIMIT_CORE) == before_core


def test_a_file_created_under_the_hardened_umask_is_owner_only(tmp_path: Path) -> None:
    restore = harden_process()
    try:
        plain = tmp_path / "plain"
        plain.write_text("x")  # created without any explicit mode
        directory = tmp_path / "d"
        directory.mkdir()
    finally:
        restore()
    assert mode(plain) == 0o600
    assert mode(directory) == 0o700


def test_a_core_limit_that_cannot_be_lowered_does_not_stop_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: object) -> None:
        raise ValueError("not allowed")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    restore = harden_process()  # no exception
    restore()


def test_the_application_hardens_the_process_for_its_lifetime(
    settings: Settings,
) -> None:
    before = current_umask()
    get_settings.cache_clear()
    app = create_app(settings)
    with TestClient(app):
        assert current_umask() == 0o077
        assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
    assert current_umask() == before
    get_settings.cache_clear()


# --- the data directory ------------------------------------------------------------------------


def test_directories_that_already_existed_with_an_open_mode_are_tightened(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    (data / "uploads").mkdir()
    (data / "uploads").chmod(0o755)
    Settings(env="dev", data_dir=data, ollama_base_url="http://127.0.0.1:1").ensure_dirs()
    assert mode(data) == 0o700
    assert mode(data / "uploads") == 0o700
    assert mode(data / "results") == 0o700


def test_init_db_tightens_an_existing_open_directory_and_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o755)
    path = data / "app.db"
    path.write_bytes(b"")
    path.chmod(0o644)
    init_db(path)
    try:
        assert mode(data) == 0o700
        assert mode(path) == 0o600
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None


# --- the database files ------------------------------------------------------------------------


def test_the_write_ahead_log_and_its_shared_memory_file_are_owner_only(tmp_path: Path) -> None:
    """They hold recently written pages, and SQLite creates them with the process's umask."""
    old = os.umask(0o022)  # an ordinary desktop umask: files would be 0644
    try:
        path = tmp_path / "data" / "app.db"
        init_db(path)
        with session_scope() as s:
            doc = Document(owner="u", display_name="a", kind="docx", size_bytes=1, sha256="0" * 64)
            s.add(doc)
        files = [Path(f"{path}{suffix}") for suffix in ("", "-wal", "-shm")]
        assert all(f.exists() for f in files), [f.name for f in files if not f.exists()]
        assert [mode(f) for f in files] == [0o600, 0o600, 0o600]
    finally:
        os.umask(old)
        dbmod._engine = None
        dbmod._SessionLocal = None


def test_secure_database_files_skips_what_does_not_exist(tmp_path: Path) -> None:
    secure_database_files(tmp_path / "nothing.db")  # no exception
    path = tmp_path / "x.db"
    path.write_bytes(b"")
    path.chmod(0o666)
    secure_database_files(path)
    assert mode(path) == 0o600


def test_every_new_connection_enforces_the_pragmas(settings: Settings) -> None:
    settings.ensure_dirs()
    engine = init_db(settings.db_path)
    try:
        with engine.connect() as conn:
            pragma = {
                name: conn.exec_driver_sql(f"PRAGMA {name}").scalar()
                for name in ("secure_delete", "trusted_schema", "cell_size_check", "foreign_keys")
            }
            mode_ = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert pragma == {
            "secure_delete": 1,
            "trusted_schema": 0,
            "cell_size_check": 1,
            "foreign_keys": 1,
        }
        assert mode_ == "wal"
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None


MARKER = "CONFIDENTIAL-QUESTION-TEXT-0451"


def _raw_bytes(db_path: Path) -> bytes:
    return b"".join(
        Path(f"{db_path}{suffix}").read_bytes()
        for suffix in ("", "-wal")
        if Path(f"{db_path}{suffix}").exists()
    )


def test_a_deleted_value_is_gone_from_the_database_files_once_checkpointed(
    settings: Settings,
) -> None:
    from procurement_agent.models import Job

    settings.ensure_dirs()
    init_db(settings.db_path)
    try:
        with session_scope() as s:
            doc = Document(owner="u", display_name="a", kind="docx", size_bytes=1, sha256="0" * 64)
            s.add(doc)
            s.flush()
            s.add(Job(document_id=doc.id, owner="u", kind="extract", params={"question": MARKER}))
        checkpoint()
        assert MARKER.encode() in _raw_bytes(settings.db_path)  # the file does hold it
        with session_scope() as s:
            for job in s.query(Job):
                s.delete(job)
        checkpoint()
        assert MARKER.encode() not in _raw_bytes(settings.db_path), "secure_delete + checkpoint"
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None


def test_checkpoint_before_init_or_on_a_closed_database_is_harmless() -> None:
    dbmod._engine = None
    checkpoint()  # no engine: nothing to do


def test_the_sqlite_files_are_really_a_database(settings: Settings) -> None:
    """Sanity for the tests above: they read raw bytes of a genuine SQLite file."""
    settings.ensure_dirs()
    init_db(settings.db_path)
    try:
        checkpoint()
        db = sqlite3.connect(settings.db_path)
        try:
            tables = {r[0] for r in db.execute("select name from sqlite_master")}
        finally:
            db.close()
        assert {"documents", "jobs", "audit_log"} <= tables
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None


# --- everything the application writes while it runs -------------------------------------------


def test_every_file_and_directory_a_running_application_creates_is_owner_only(
    tmp_path: Path,
) -> None:
    import io
    import time

    from docx import Document as WordDocument

    old = os.umask(0o022)  # as a developer's shell has it
    try:
        settings = Settings(
            env="dev",
            data_dir=tmp_path / "data",
            dev_api_key=KEY,
            ollama_base_url="http://127.0.0.1:1",
            rate_limit="10000/minute",
        )
        get_settings.cache_clear()
        app = create_app(settings)
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            word = WordDocument()
            word.add_paragraph("SUPPLY CONTRACT No. 1")
            buf = io.BytesIO()
            word.save(buf)
            files = {"file": ("a.docx", buf.getvalue())}
            r = client.post("/api/v1/documents", headers={"X-API-Key": KEY}, files=files)
            assert r.status_code == 201
            job = r.json()["parse_job_id"]
            for _ in range(600):
                status = client.get(f"/api/v1/jobs/{job}", headers={"X-API-Key": KEY}).json()[
                    "status"
                ]
                if status in ("done", "failed"):
                    break
                time.sleep(0.05)
            assert status == "done"
            client.post(
                f"/api/v1/documents/{r.json()['document']['id']}/extract",
                headers={"X-API-Key": KEY},
                json={},
            )
            time.sleep(1.5)
        loose = [
            (str(p.relative_to(tmp_path)), oct(mode(p)))
            for p in [settings.data_dir, *settings.data_dir.rglob("*")]
            if mode(p) & 0o077
        ]
        assert loose == [], "readable by group or others"
        assert any(p.name.endswith(".json") for p in settings.results_dir.rglob("*")), (
            "nothing was written"
        )
    finally:
        os.umask(old)
        get_settings.cache_clear()
        dbmod._engine = None
        dbmod._SessionLocal = None

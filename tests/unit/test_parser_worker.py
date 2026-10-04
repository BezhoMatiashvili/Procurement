"""Parse jobs through the parser socket, in the worker; and the same jobs inline, unchanged.

A parser that fails, hangs, dies or is not there fails *that* job with a code and a fixed sentence,
is not retried, and does not stop the next job. Inline mode - the default, and what every other test
in the suite runs - makes exactly the two calls it always made.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docling.datamodel.base_models import ConversionStatus
from docling_core.types.doc.document import DoclingDocument
from fastapi.testclient import TestClient
from sqlalchemy import select

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import init_db, session_scope
from procurement_agent.extraction.pipeline import result_dir, upload_path
from procurement_agent.ingestion import docling_service, parser_client, read_quality
from procurement_agent.jobs import worker as worker_mod
from procurement_agent.jobs.worker import Worker
from procurement_agent.main import create_app
from procurement_agent.models import AuditLog, Document, DocumentStatus, Job, JobStatus
from procurement_agent.parser_service import server as srv
from procurement_agent.security import egress
from tests.unit.parser_harness import (
    Harness,
    LogRecorder,
    converter_of,
    docling_json,
    readable_docling_json,
)

CANARY = "CONFIDENTIAL-CLIENT-TEXT-ბარამიძე"


def make_settings(tmp_path: Path, socket_path: Path | None, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "dev",
        "data_dir": tmp_path / "data",
        "ollama_base_url": "http://127.0.0.1:1",
        "parser_connect_wait_s": 0.3,
    }
    if socket_path is not None:
        base.update(parser_mode="socket", parser_socket=socket_path)
    base.update(overrides)
    s = Settings(**base)
    s.ensure_dirs()
    return s


def setup_db(settings: Settings) -> None:
    init_db(settings.db_path)


def teardown_db() -> None:
    dbmod._engine = None
    dbmod._SessionLocal = None


def make_job(settings: Settings, kind: str = "pdf") -> tuple[str, str]:
    with session_scope() as s:
        doc = Document(
            owner="u",
            display_name="a." + kind,
            kind=kind,
            size_bytes=10,
            sha256="0" * 64,
            status=DocumentStatus.uploaded,
        )
        s.add(doc)
        s.flush()
        job = Job(document_id=doc.id, owner="u", kind="parse", params={})
        s.add(job)
        s.flush()
        ids = (doc.id, job.id)
    upload_path(settings, ids[0], kind).write_bytes(
        b"%PDF-1.4 the upload" if kind == "pdf" else b"PK"
    )
    return ids


def state(doc_id: str, job_id: str) -> tuple[Document, Job]:
    with session_scope() as s:
        doc, job = s.get(Document, doc_id), s.get(Job, job_id)
        assert doc is not None and job is not None
        s.expunge_all()
        return doc, job


@pytest.fixture
def worker_env(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path
    teardown_db()


def worker_for(settings: Settings) -> Worker:
    setup_db(settings)
    return Worker(settings, None)


# --- socket mode -----------------------------------------------------------------------------


def test_a_parse_job_is_done_through_the_parser_and_stores_the_parsers_document(
    worker_env: Path, sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("Docling ran in the web process")

    monkeypatch.setattr(docling_service, "convert", forbidden)
    with Harness(
        sock_dir, converter_of(4, "partial_success", extra="ბარამიძე", readable=True)
    ) as h:
        settings = make_settings(worker_env, h.path)
        w = worker_for(settings)
        doc_id, job_id = make_job(settings)
        w._process(job_id)
    doc, job = state(doc_id, job_id)
    assert (job.status, job.message, job.progress) == (JobStatus.done, "ready", 100)
    assert (doc.status, doc.page_count, doc.error) == (DocumentStatus.ready, 4, None)
    stored = (result_dir(settings, doc_id) / "docling.json").read_bytes()
    assert stored == readable_docling_json(4, extra="ბარამიძე")
    quality = read_quality.read_sidecar(settings, doc_id)
    assert quality is not None and quality.partial, "the parser's partial_success is carried over"


def test_a_docx_has_no_page_count_and_is_stored(worker_env: Path, sock_dir: Path) -> None:
    with Harness(sock_dir, converter_of(0, readable=True)) as h:
        settings = make_settings(worker_env, h.path)
        w = worker_for(settings)
        doc_id, job_id = make_job(settings, "docx")
        w._process(job_id)
    doc, job = state(doc_id, job_id)
    assert job.status is JobStatus.done and doc.status is DocumentStatus.ready
    assert doc.page_count is None


def test_the_worker_shows_it_is_alive_while_the_parser_works(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(worker_env, worker_env / "x.sock")
    w = worker_for(settings)
    seen: dict[str, Any] = {}

    def fake(
        path: Path, kind: str, out_dir: Path, s: Settings, **kw: Any
    ) -> parser_client.ParsedReply:
        seen.update(kw, path=path, kind=kind, out_dir=out_dir)
        docling_service.save_document_bytes(readable_docling_json(3), out_dir)
        return parser_client.ParsedReply(pages=3, partial=False)

    monkeypatch.setattr(parser_client, "convert_through_parser", fake)
    doc_id, job_id = make_job(settings)
    w._process(job_id)
    assert seen["beat"] == w._beat
    assert seen["out_dir"] == result_dir(settings, doc_id)
    assert seen["path"] == upload_path(settings, doc_id, "pdf") and seen["kind"] == "pdf"
    assert state(doc_id, job_id)[0].page_count == 3


def run_failing(worker_env: Path, sock_dir: Path, harness: Harness) -> tuple[Document, Job, Path]:
    settings = make_settings(worker_env, harness.path)
    w = worker_for(settings)
    doc_id, job_id = make_job(settings)
    w._run_one(job_id)
    doc, job = state(doc_id, job_id)
    return doc, job, result_dir(settings, doc_id)


def test_a_parser_that_fails_fails_that_job_with_a_code_and_the_next_job_is_served(
    worker_env: Path, sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [0]

    def converter(path: Path, kind: str) -> srv.Converted:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError(f"cannot read {CANARY}")
        return srv.Converted(1, "success", readable_docling_json(1))

    recorder = LogRecorder()
    monkeypatch.setattr(worker_mod, "log", recorder)
    with Harness(sock_dir, converter) as h:
        settings = make_settings(worker_env, h.path)
        w = worker_for(settings)
        first = make_job(settings)
        second = make_job(settings)
        w._run_one(first[1])
        w._run_one(second[1])
    doc, job = state(*first)
    assert job.status is JobStatus.failed and doc.status is DocumentStatus.failed
    assert job.message is not None and "PARSER_CONVERT_FAILED" in job.message
    assert doc.error is not None and "PARSER_CONVERT_FAILED" in doc.error
    assert not (result_dir(settings, first[0]) / "docling.json").exists()
    doc2, job2 = state(*second)
    assert job2.status is JobStatus.done and doc2.status is DocumentStatus.ready
    assert calls[0] == 2, "the failed document was sent once"
    assert any(e["event"] == "parse_failed" for e in recorder.events)
    assert CANARY not in recorder.text() and CANARY not in (job.message or "") + (doc.error or "")
    with session_scope() as s:
        rows = [r.detail or "" for r in s.scalars(select(AuditLog))]
    assert any("PARSER_CONVERT_FAILED" in r for r in rows)
    assert all(CANARY not in r for r in rows)


def test_a_parser_that_stops_answering_fails_the_job_as_a_timeout(
    worker_env: Path, sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def stuck(path: Path, kind: str) -> srv.Converted:
        release.wait(5)
        return srv.Converted(1, "success", docling_json(1))

    monkeypatch.setattr(parser_client, "default_reply_timeout_s", lambda s: 0.5)
    with Harness(sock_dir, stuck) as h:
        doc, job, out = run_failing(worker_env, sock_dir, h)
        release.set()
    assert job.status is JobStatus.failed
    assert job.message is not None and "PARSER_TIMEOUT" in job.message
    assert doc.status is DocumentStatus.failed and not (out / "docling.json").exists()


def test_a_parser_that_dies_fails_the_job_as_a_crash_and_is_not_asked_again(
    worker_env: Path, sock_dir: Path
) -> None:
    path = sock_dir / "dies.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.settimeout(0.2)
    connections: list[int] = []
    done = threading.Event()

    def die() -> None:
        while not done.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            connections.append(1)
            conn.close()  # reads nothing, answers nothing: a process that was killed

    t = threading.Thread(target=die, daemon=True)
    t.start()
    try:
        settings = make_settings(worker_env, path, parser_connect_wait_s=30.0)
        w = worker_for(settings)
        doc_id, job_id = make_job(settings)
        w._run_one(job_id)
        time.sleep(0.5)
    finally:
        done.set()
        t.join(5)
        listener.close()
    doc, job = state(doc_id, job_id)
    assert job.status is JobStatus.failed
    assert job.message is not None and "PARSER_CRASHED" in job.message
    assert doc.status is DocumentStatus.failed
    assert len(connections) == 1, "no retry of a document that was sent"
    assert w.queue_size == 0


def test_no_parser_at_all_fails_the_job_as_unavailable(worker_env: Path) -> None:
    path = worker_env / "nobody.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    settings = make_settings(worker_env, path, parser_connect_wait_s=0.2)
    w = worker_for(settings)
    doc_id, job_id = make_job(settings)
    w._run_one(job_id)
    doc, job = state(doc_id, job_id)
    assert job.status is JobStatus.failed
    assert job.message is not None and "PARSER_UNAVAILABLE" in job.message
    assert doc.status is DocumentStatus.failed


def test_a_document_deleted_while_the_parser_works_leaves_nothing_behind(
    worker_env: Path, sock_dir: Path
) -> None:
    from procurement_agent.jobs.retention import delete_document_files
    from procurement_agent.jobs.worker import mark_deleted

    gate = threading.Event()
    release = threading.Event()

    def slow(path: Path, kind: str) -> srv.Converted:
        gate.set()
        release.wait(5)
        return srv.Converted(1, "success", docling_json(1))

    with Harness(sock_dir, slow) as h:
        settings = make_settings(worker_env, h.path)
        w = worker_for(settings)
        doc_id, job_id = make_job(settings)
        t = threading.Thread(target=w._run_one, args=(job_id,))
        t.start()
        assert gate.wait(5)
        with session_scope() as sess:
            found = sess.get(Document, doc_id)
            assert found is not None
            mark_deleted(found)
        delete_document_files(settings, doc_id, "pdf")
        release.set()
        t.join(10)
    assert not (result_dir(settings, doc_id) / "docling.json").exists()
    assert not upload_path(settings, doc_id, "pdf").exists()
    doc, job = state(doc_id, job_id)
    assert doc.status is DocumentStatus.deleted and job.status is JobStatus.cancelled


# --- inline mode -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [None, "inline"])
def test_inline_mode_makes_the_two_calls_it_always_made(
    worker_env: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    saved: list[Path] = []

    doc = DoclingDocument.model_validate_json(readable_docling_json(2))
    converted = SimpleNamespace(document=doc, status=ConversionStatus.SUCCESS, errors=[])
    monkeypatch.setattr(docling_service, "convert", lambda path, s: converted)
    monkeypatch.setattr(
        docling_service, "save_document", lambda doc, out: saved.append(out) or out / "docling.json"
    )

    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("the parser socket was used in inline mode")

    monkeypatch.setattr(parser_client, "convert_through_parser", forbidden)
    settings = make_settings(worker_env, None, parser_mode=mode)
    w = worker_for(settings)
    doc_id, job_id = make_job(settings)
    w._process(job_id)
    doc, job = state(doc_id, job_id)
    assert job.status is JobStatus.done and doc.page_count == 2
    assert saved == [result_dir(settings, doc_id)]


# --- the application hands the guard the parser's address, and only in socket mode -----------


def _app(tmp_path: Path, **overrides: Any) -> Iterator[TestClient]:
    settings = make_settings(tmp_path, None, rate_limit="10000/minute", **overrides)
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app):
        yield app
    teardown_db()
    get_settings.cache_clear()


def test_socket_mode_allows_the_parsers_socket_to_the_egress_guard_and_no_other(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pp.sock"
    gen = _app(tmp_path, parser_mode="socket", parser_socket=path)
    next(gen)
    try:
        guard = egress.installed()
        assert guard is not None and str(path) in guard.unix_paths
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_inline_mode_allows_no_unix_socket(tmp_path: Path) -> None:
    path = tmp_path / "inline.sock"
    gen = _app(tmp_path, parser_mode="inline", parser_socket=path)
    next(gen)
    try:
        guard = egress.installed()
        assert guard is not None and str(path) not in guard.unix_paths
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_production_in_inline_mode_says_so_in_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gen = _app(
        tmp_path,
        env="prod",
        parser_mode="inline",
        allowed_hosts=("tasks.company.test",),
        docling_artifacts_path=tmp_path / "models",
        models_manifest_required=False,
    )
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert "parser_inline_in_production" in capsys.readouterr().out


def test_production_with_the_parser_socket_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gen = _app(
        tmp_path,
        env="prod",
        parser_mode="socket",
        parser_socket=tmp_path / "pp.sock",
        allowed_hosts=("tasks.company.test",),
        docling_artifacts_path=tmp_path / "models",
        models_manifest_required=False,
    )
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert "parser_inline_in_production" not in capsys.readouterr().out

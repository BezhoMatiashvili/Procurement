"""Deleting and expiring documents: what goes, what is retried, what is left alone.

The failure this file exists for: the database row says ``deleted`` before the files are touched,
and the sweep that follows only selects documents that are *not* deleted. One removal that raised
(a permission error, a file busy) used to abort the loop, and every file after it - and that one -
stayed on the disk for good.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import _lru_cache_wrapper
from pathlib import Path

import pytest
from sqlalchemy import select

import procurement_agent
import procurement_agent.db as dbmod
from procurement_agent.checks import aoa
from procurement_agent.config import Settings
from procurement_agent.db import init_db, session_scope
from procurement_agent.extraction.pipeline import result_dir, upload_path
from procurement_agent.jobs import retention
from procurement_agent.jobs.retention import (
    GRACE_S,
    delete_document_files,
    forget_derived_outputs,
    overwrite,
    reconcile,
)
from procurement_agent.jobs.worker import Worker, mark_deleted
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security import caches

MARKER = b"CONFIDENTIAL-CLIENT-TEXT"


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    s = Settings(env="dev", data_dir=tmp_path / "data", ollama_base_url="http://127.0.0.1:1")
    s.ensure_dirs()
    init_db(s.db_path)
    yield s
    dbmod._engine = None
    dbmod._SessionLocal = None


def _doc(
    settings: Settings,
    *,
    status: DocumentStatus = DocumentStatus.ready,
    expired: bool = False,
    kind: str = "docx",
    with_files: bool = True,
) -> str:
    with session_scope() as s:
        doc = Document(
            owner="u",
            display_name="a.docx",
            kind=kind,
            size_bytes=10,
            sha256="0" * 64,
            status=status,
            expires_at=datetime.now(UTC) - timedelta(days=1) if expired else None,
        )
        s.add(doc)
        s.flush()
        doc_id = doc.id
    if with_files:
        _files(settings, doc_id, kind)
    return doc_id


def _files(settings: Settings, doc_id: str, kind: str = "docx") -> None:
    upload_path(settings, doc_id, kind).write_bytes(MARKER * 100)
    results = result_dir(settings, doc_id)
    (results / "jobs").mkdir(parents=True, exist_ok=True)
    (results / "docling.json").write_bytes(MARKER * 100)
    (results / "jobs" / "x.json").write_bytes(MARKER)


def _exists(settings: Settings, doc_id: str, kind: str = "docx") -> tuple[bool, bool]:
    return upload_path(settings, doc_id, kind).exists(), result_dir(settings, doc_id).exists()


def _old(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _worker(settings: Settings) -> Worker:
    return Worker(settings, None)


# --- overwrite ---------------------------------------------------------------------------------


def test_overwrite_zeroes_every_block_and_syncs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(MARKER * 200_000)  # about 4.8 MB: several blocks
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    size = path.stat().st_size
    overwrite(path)
    data = path.read_bytes()
    assert len(data) == size
    assert data == b"\0" * size, "the whole file is zeroed, not the first block"
    assert len(synced) == 1


def test_overwrite_survives_a_file_that_is_gone_or_unwritable(tmp_path: Path) -> None:
    overwrite(tmp_path / "missing")  # no exception
    path = tmp_path / "ro"
    path.write_bytes(b"x")
    path.chmod(0o400)
    overwrite(path)  # still no exception; unlinking is the caller's step
    assert path.read_bytes() == b"x"


# --- delete_document_files ---------------------------------------------------------------------


def test_everything_of_a_document_goes_and_the_caller_is_told(settings: Settings) -> None:
    doc_id = _doc(settings)
    assert delete_document_files(settings, doc_id, "docx") is True
    assert _exists(settings, doc_id) == (False, False)


def test_a_file_that_will_not_go_does_not_stop_the_rest(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _doc(settings)
    real = retention._remove_file

    def refuse_the_upload(path: Path) -> None:
        if path.parent == settings.upload_dir:
            raise PermissionError(13, "denied", str(path))
        real(path)

    monkeypatch.setattr(retention, "_remove_file", refuse_the_upload)
    assert delete_document_files(settings, doc_id, "docx") is False
    upload_left, results_left = _exists(settings, doc_id)
    assert upload_left is True  # the one that was refused is still there...
    assert results_left is False  # ...and the results were removed regardless


def test_a_failure_is_logged_by_kind_never_by_path(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc_id = _doc(settings)
    monkeypatch.setattr(
        retention, "_remove_file", lambda p: (_ for _ in ()).throw(PermissionError(13, "x", str(p)))
    )
    delete_document_files(settings, doc_id, "docx")
    out = capsys.readouterr().out
    assert "retention_delete_failed" in out
    assert "PermissionError(errno=EACCES)" in out
    assert str(settings.upload_dir) not in out


def test_deleting_a_document_that_has_no_files_is_complete(settings: Settings) -> None:
    doc_id = _doc(settings, with_files=False)
    assert delete_document_files(settings, doc_id, "docx") is True


# --- the sweep ---------------------------------------------------------------------------------


def test_one_failing_document_does_not_strand_the_rest_and_is_retried(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [_doc(settings, expired=True) for _ in range(3)]
    real = retention._remove_file
    broken = {"on": True}

    def flaky(path: Path) -> None:
        if broken["on"] and ids[1] in path.name:
            raise PermissionError(13, "denied")
        real(path)

    monkeypatch.setattr(retention, "_remove_file", flaky)
    worker = _worker(settings)
    assert worker.sweep_expired() == 3
    assert _exists(settings, ids[0]) == (False, False)
    assert _exists(settings, ids[2]) == (False, False)
    assert _exists(settings, ids[1])[0] is True, "the refused one is still there"
    with session_scope() as s:  # ...and its row is final, which is the trap
        assert s.get(Document, ids[1]).status == DocumentStatus.deleted  # type: ignore[union-attr]
    # the next sweep selects no document at all; the reconciler finishes the job anyway
    broken["on"] = False
    assert worker.sweep_expired() == 0
    assert _exists(settings, ids[1]) == (False, False)


def test_the_sweep_forgets_what_users_typed_into_the_jobs_of_expired_documents(
    settings: Settings,
) -> None:
    doc_id = _doc(settings, expired=True)
    with session_scope() as s:
        s.add(
            Job(
                document_id=doc_id,
                owner="u",
                kind="extract",
                params={"question": "What is the supplier's bank account?"},
                status=JobStatus.done,
            )
        )
    _worker(settings).sweep_expired()
    with session_scope() as s:
        assert [j.params for j in s.scalars(select(Job))] == [{}]


# --- the reconciler ----------------------------------------------------------------------------


def test_the_files_of_a_deleted_document_are_removed_at_once_whatever_their_age(
    settings: Settings,
) -> None:
    doc_id = _doc(settings, status=DocumentStatus.deleted)
    assert reconcile(settings).total >= 2
    assert _exists(settings, doc_id) == (False, False)


def test_live_documents_are_left_alone_in_every_state(settings: Settings) -> None:
    ids = [
        _doc(settings, status=st)
        for st in (
            DocumentStatus.uploaded,
            DocumentStatus.parsing,
            DocumentStatus.ready,
            DocumentStatus.failed,
        )
    ]
    for doc_id in ids:
        _old(upload_path(settings, doc_id, "docx"), 10 * GRACE_S)
        _old(result_dir(settings, doc_id), 10 * GRACE_S)
    found = reconcile(settings)
    assert found.total == 0
    assert all(_exists(settings, d) == (True, True) for d in ids)


def test_a_file_with_no_row_is_an_orphan_only_after_the_grace_period(settings: Settings) -> None:
    """An upload writes its file after the row is flushed and before the commit."""
    fresh, stale = "a" * 32, "b" * 32
    for doc_id in (fresh, stale):
        _files(settings, doc_id)
    _old(upload_path(settings, stale, "docx"), GRACE_S + 60)
    _old(result_dir(settings, stale), GRACE_S + 60)
    reconcile(settings)
    assert _exists(settings, fresh) == (True, True)
    assert _exists(settings, stale) == (False, False)


def test_only_files_this_application_names_are_ever_touched(settings: Settings) -> None:
    stray = [
        settings.upload_dir / "notes.txt",
        settings.upload_dir / "README",
        settings.upload_dir / (("c" * 32) + ".docx.bak"),
        settings.upload_dir / "UPPERCASE".ljust(32, "A"),
    ]
    for path in stray:
        path.write_bytes(b"operator's file")
        _old(path, 10 * GRACE_S)
    odd_dir = settings.results_dir / "not-a-document-id"
    odd_dir.mkdir()
    _old(odd_dir, 10 * GRACE_S)
    reconcile(settings)
    assert all(p.exists() for p in stray)
    assert odd_dir.exists()


def test_a_symlink_is_never_followed_or_removed_through(settings: Settings, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours")
    doc_id = _doc(settings, status=DocumentStatus.deleted, with_files=False)
    link_dir = settings.results_dir / doc_id
    link_dir.symlink_to(outside, target_is_directory=True)
    link_file = upload_path(settings, doc_id, "docx")
    link_file.symlink_to(outside / "keep.txt")
    reconcile(settings)
    assert (outside / "keep.txt").read_text() == "not ours"
    assert link_dir.is_symlink() and link_file.is_symlink()


def test_a_removal_that_fails_is_counted_and_does_not_stop_the_pass(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [_doc(settings, status=DocumentStatus.deleted) for _ in range(3)]
    real = retention._remove_file
    monkeypatch.setattr(
        retention,
        "_remove_file",
        lambda p: (_ for _ in ()).throw(OSError(5, "io")) if ids[0] in p.name else real(p),
    )
    found = reconcile(settings)
    assert found.failed == 1
    assert _exists(settings, ids[0])[0] is True
    assert _exists(settings, ids[1]) == (False, False)
    assert _exists(settings, ids[2]) == (False, False)


def test_half_written_parse_output_is_debris_after_an_hour(settings: Settings) -> None:
    doc_id = _doc(settings)
    old = result_dir(settings, doc_id) / ".docling.abc123.json.tmp"
    new = result_dir(settings, doc_id) / ".docling.def456.json.tmp"
    for path in (old, new):
        path.write_bytes(MARKER)
    _old(old, retention.STALE_TMP_S + 60)
    found = reconcile(settings)
    assert found.debris == 1
    assert not old.exists() and new.exists()
    assert (result_dir(settings, doc_id) / "docling.json").exists()


# --- derived outputs ---------------------------------------------------------------------------


def _explain_job(settings: Settings, right: str, left: str, *, output: bool = True) -> str:
    with session_scope() as s:
        job = Job(
            document_id=right,
            owner="u",
            kind="compare_explain",
            params={"left": left, "note": "kept?"},
            status=JobStatus.done,
        )
        s.add(job)
        s.flush()
        job_id = job.id
    if output:
        out = result_dir(settings, right) / "jobs"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{job_id}.json").write_bytes(MARKER)
    return job_id


def test_the_output_that_quotes_a_deleted_document_goes_with_it(settings: Settings) -> None:
    left, right = _doc(settings), _doc(settings)
    job_id = _explain_job(settings, right, left)
    output = result_dir(settings, right) / "jobs" / f"{job_id}.json"
    assert output.exists()
    assert delete_document_files(settings, left, "docx") is True
    assert not output.exists(), "the right-hand document's output quoted the deleted one"
    assert (result_dir(settings, right) / "docling.json").exists(), "the right-hand document stays"
    with session_scope() as s:
        assert s.get(Job, job_id).params == {}  # type: ignore[union-attr]


def test_an_aoa_review_follows_its_contract(settings: Settings) -> None:
    act, contract = _doc(settings), _doc(settings)
    with session_scope() as s:
        job = Job(
            document_id=act,
            owner="u",
            kind="aoa_review",
            params={"contract": contract},
            status=JobStatus.done,
        )
        s.add(job)
        s.flush()
        job_id = job.id
    out = result_dir(settings, act) / "jobs"
    (out / f"{job_id}.json").write_bytes(MARKER)
    forget_derived_outputs(settings, contract)
    assert not (out / f"{job_id}.json").exists()


def test_the_reconciler_removes_a_derived_output_a_deletion_missed(settings: Settings) -> None:
    left, right = _doc(settings), _doc(settings)
    job_id = _explain_job(settings, right, left)
    with session_scope() as s:  # the row was marked deleted, and the derived cleanup never ran
        doc = s.get(Document, left)
        assert doc is not None
        doc.status = DocumentStatus.deleted
    found = reconcile(settings)
    assert found.derived == 1
    assert not (result_dir(settings, right) / "jobs" / f"{job_id}.json").exists()


def test_a_job_that_has_not_finished_keeps_its_parameters(settings: Settings) -> None:
    """The worker reads them to notice that its document is gone, and cancels the job."""
    left, right = _doc(settings), _doc(settings)
    queued = _explain_job(settings, right, left, output=False)
    with session_scope() as s:
        job = s.get(Job, queued)
        assert job is not None
        job.status = JobStatus.queued
    delete_document_files(settings, left, "docx")
    reconcile(settings)
    with session_scope() as s:
        assert s.get(Job, queued).params["left"] == left  # type: ignore[union-attr]


def test_other_jobs_outputs_are_untouched(settings: Settings) -> None:
    left, right, other = _doc(settings), _doc(settings), _doc(settings)
    unrelated = _explain_job(settings, right, other)
    _explain_job(settings, right, left)
    delete_document_files(settings, left, "docx")
    assert (result_dir(settings, right) / "jobs" / f"{unrelated}.json").exists()


# --- mark_deleted and the caches ---------------------------------------------------------------


def test_mark_deleted_scrubs_the_jobs_of_the_document_and_empties_the_text_caches(
    settings: Settings,
) -> None:
    doc_id = _doc(settings)
    with session_scope() as s:
        s.add(
            Job(
                document_id=doc_id,
                owner="u",
                kind="extract",
                params={"question": "q?"},
                status=JobStatus.done,
            )
        )
    aoa._period_opens("some period text from a document 01.01.2024")
    assert aoa._period_opens.cache_info().currsize >= 1
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        assert doc is not None
        mark_deleted(doc)
    assert aoa._period_opens.cache_info().currsize == 0
    with session_scope() as s:
        assert [j.params for j in s.scalars(select(Job))] == [{}]


def test_forget_cached_text_reports_what_it_dropped() -> None:
    aoa._period_opens("a document's text")
    aoa._slipped_signing("a document's text")
    assert caches.forget_cached_text() >= 2
    assert caches.forget_cached_text() == 0


def test_every_cache_in_the_application_is_classified() -> None:
    """A new ``lru_cache`` is either keyed on text (and emptied on deletion) or provably not."""
    found: set[tuple[str, str]] = set()
    for module in pkgutil.walk_packages(procurement_agent.__path__, "procurement_agent."):
        if ".eval." in module.name or module.name.endswith(".cli"):
            continue
        loaded = importlib.import_module(module.name)
        for name, value in vars(loaded).items():
            if isinstance(value, _lru_cache_wrapper) and value.__module__ == module.name:
                found.add((module.name, name))
    assert found, "the scan found no caches: it is looking in the wrong place"
    classified = set(caches.TEXT_KEYED) | caches.NO_TEXT
    assert found == classified, f"unclassified: {found - classified}; stale: {classified - found}"


def test_forget_cached_text_empties_exactly_the_text_keyed_caches() -> None:
    cached = [getattr(importlib.import_module(m), n) for m, n in caches.TEXT_KEYED]
    aoa._period_opens("period text 1")
    aoa._slipped_signing("signing text 1")
    assert all(c.cache_info().currsize >= 1 for c in cached)
    caches.forget_cached_text()
    assert all(c.cache_info().currsize == 0 for c in cached)

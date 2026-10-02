"""Removing what a deletion or an expiry is supposed to remove - and noticing what it missed.

A document's database row says ``deleted`` *before* its files are touched (``jobs.worker`` explains
why). That ordering has a price: when the file removal then fails - a permission error, a file busy,
a crash between the commit and the unlink - the row is final, the sweep that follows only selects
documents that are *not* deleted, and the plaintext stays on the disk for good. So:

* ``delete_document_files`` never stops at the first problem and says whether everything went;
* ``reconcile`` is the other half: it finds files whose document is deleted - or is not in the
  database at all - and removes them, on every sweep, so a failed removal is retried until it works;
* the outputs of explain/review jobs live under *another* document's directory (the right-hand
  document, the act) and quote the one that was deleted; those go with it.

A file whose document is unknown is only removed once it is old enough (``GRACE``): an upload writes
its file after the row is flushed and before the commit, and must not lose it to a sweep.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from procurement_agent.config import Settings
from procurement_agent.db import session_scope
from procurement_agent.extraction.pipeline import result_dir, upload_path
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import log
from procurement_agent.security.safe_errors import safe_exc

__all__ = ["GRACE_S", "Reconciled", "delete_document_files", "overwrite", "reconcile"]

#: A file whose document is not in the database is an orphan only after this long.
GRACE_S = 15 * 60
#: Half-written parse output (``docling_service.save_document``) older than this is debris.
STALE_TMP_S = 60 * 60
#: Overwritten in blocks this size, so a large file is not read into memory.
_BLOCK = 1024 * 1024
#: What this application names its files: a document id, with the kind for an upload. Anything
#: else in the data directory is not ours to remove.
_UPLOAD_NAME = re.compile(r"([0-9a-f]{32})\.(?:pdf|docx|xlsx)")
_RESULT_NAME = re.compile(r"[0-9a-f]{32}")
#: Jobs whose output lives under one document and quotes another, and the parameter that names it.
_DERIVED = {"compare_explain": "left", "aoa_review": "contract"}
#: Only a job that has finished may be scrubbed: a queued or running one still needs its parameters
#: to notice that its document is gone, and cancels itself (``Worker._compare_explain``).
_FINISHED = (JobStatus.done, JobStatus.failed, JobStatus.cancelled)


def overwrite(path: Path) -> None:
    """Zero a file's blocks, flush them to the device, before it is unlinked.

    Best effort, and said so: on a copy-on-write filesystem or an SSD the old blocks may survive,
    which is why the deployment's protection is an encrypted disk (docs/SECURITY.md). It costs a
    few milliseconds and removes the plain read of an undeleted-file tool on an ordinary one.
    """
    try:
        remaining = path.stat().st_size
        with path.open("r+b") as fh:
            while remaining > 0:
                fh.write(b"\0" * min(remaining, _BLOCK))
                remaining -= _BLOCK
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def _remove_file(path: Path) -> None:
    overwrite(path)
    path.unlink(missing_ok=True)


def _remove_tree(directory: Path) -> None:
    for f in directory.rglob("*"):
        if f.is_file() and not f.is_symlink():
            overwrite(f)
    shutil.rmtree(directory)  # raises, so the caller sees a directory that would not go


def delete_document_files(settings: Settings, doc_id: str, kind: str) -> bool:
    """Remove a document's upload and results, and the outputs of other documents' jobs that
    quote it. Keeps going past a failure; returns True only when nothing is left."""
    complete = True
    for action in (
        lambda: _remove_file(upload_path(settings, doc_id, kind)),
        lambda: (
            _remove_tree(result_dir(settings, doc_id))
            if result_dir(settings, doc_id).exists()
            else None
        ),
        lambda: forget_derived_outputs(settings, doc_id),
    ):
        try:
            action()
        except Exception as e:
            complete = False
            log.error("retention_delete_failed", document_id=doc_id, error=safe_exc(e))
    return complete


def forget_derived_outputs(settings: Settings, doc_id: str) -> int:
    """Delete the stored output of every explain/review job that quotes ``doc_id``, and drop what
    the user typed into those jobs. Returns how many outputs were removed."""
    removed = 0
    with session_scope() as s:
        finished = select(Job).where(Job.kind.in_(tuple(_DERIVED)), Job.status.in_(_FINISHED))
        for job in s.scalars(finished):
            if job.params.get(_DERIVED[job.kind]) != doc_id:
                continue
            output = result_dir(settings, job.document_id) / "jobs" / f"{job.id}.json"
            if output.exists():
                _remove_file(output)
                removed += 1
            job.params = {}
    return removed


@dataclass
class Reconciled:
    """What a pass found: counts only."""

    files: int = 0
    directories: int = 0
    derived: int = 0
    debris: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.files + self.directories + self.derived + self.debris


def _age(path: Path, now: float) -> float:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0.0


def reconcile(
    settings: Settings, *, now: float | None = None, grace_s: float = GRACE_S
) -> Reconciled:
    """Remove files and directories that belong to no live document.

    Deleted document: removed at once. Unknown document: removed once it is older than
    ``grace_s``. Also: outputs of explain/review jobs whose quoted document is deleted, and
    half-written parse output left by a crash.
    """
    now = time.time() if now is None else now
    found = Reconciled()
    with session_scope() as s:
        status = dict(s.execute(select(Document.id, Document.status)).tuples().all())
        derived = [
            (j.id, j.document_id, j.params.get(_DERIVED[j.kind]))
            for j in s.scalars(
                select(Job).where(Job.kind.in_(tuple(_DERIVED)), Job.status.in_(_FINISHED))
            )
        ]

    def orphan(doc_id: str, path: Path) -> bool:
        state = status.get(doc_id)
        if state == DocumentStatus.deleted:
            return True
        return state is None and _age(path, now) > grace_s

    def attempt(path: Path, remover: object) -> bool:
        try:
            remover(path)  # type: ignore[operator]
        except Exception as e:
            found.failed += 1
            log.error("reconcile_failed", error=safe_exc(e))
            return False
        return True

    if settings.upload_dir.is_dir():
        for path in settings.upload_dir.iterdir():
            named = _UPLOAD_NAME.fullmatch(path.name)
            if named and path.is_file() and not path.is_symlink() and orphan(named.group(1), path):
                found.files += attempt(path, _remove_file)
    if settings.results_dir.is_dir():
        for path in settings.results_dir.iterdir():
            if (
                _RESULT_NAME.fullmatch(path.name)
                and path.is_dir()
                and not path.is_symlink()
                and orphan(path.name, path)
            ):
                found.directories += attempt(path, _remove_tree)
    for job_id, document_id, quoted in derived:
        if status.get(str(quoted)) != DocumentStatus.deleted:
            continue
        output = result_dir(settings, document_id) / "jobs" / f"{job_id}.json"
        if output.exists():
            found.derived += attempt(output, _remove_file)
    if settings.results_dir.is_dir():
        for tmp in settings.results_dir.glob("*/.docling.*.json.tmp"):
            if _age(tmp, now) > STALE_TMP_S:
                found.debris += attempt(tmp, _remove_file)
    if found.total or found.failed:
        log.info(
            "retention_reconciled",
            files=found.files,
            directories=found.directories,
            derived=found.derived,
            debris=found.debris,
            failed=found.failed,
        )
    return found

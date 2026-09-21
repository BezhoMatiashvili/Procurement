"""Single background worker: parses documents, runs extractions, enforces retention.

One thread is deliberate — on a CPU-only box, Docling and the LLM would only fight each other.
All state lives in the database so the API can report progress and a restart loses nothing but
the currently running job (which is marked failed on startup).
"""

from __future__ import annotations

import queue
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from procurement_agent.config import Settings
from procurement_agent.db import session_scope
from procurement_agent.extraction.pipeline import (
    load_chunks,
    result_dir,
    run_extraction,
    upload_path,
)
from procurement_agent.extraction.schemas import FactType
from procurement_agent.ingestion import docling_service
from procurement_agent.llm.base import LLMClient
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import audit, log

_SWEEP_EVERY_S = 600
_STOP = object()


class Worker:
    def __init__(self, settings: Settings, llm: LLMClient | None) -> None:
        self._s = settings
        self._llm = llm
        self._q: queue.Queue[object] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="pa-worker", daemon=True)
        self._last_sweep = 0.0

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        self._recover()
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._q.put(_STOP)
        self._thread.join(timeout)

    def submit(self, job_id: str) -> None:
        self._q.put(job_id)

    @property
    def queue_size(self) -> int:
        return self._q.qsize()

    # --- internals -----------------------------------------------------------------------------

    def _recover(self) -> None:
        """Jobs left running by a previous process cannot finish: fail them, re-queue queued."""
        with session_scope() as s:
            for job in s.scalars(select(Job).where(Job.status == JobStatus.running)):
                job.status = JobStatus.failed
                job.message = "interrupted by restart"
                job.finished_at = datetime.now(UTC)
            queued = [j.id for j in s.scalars(select(Job).where(Job.status == JobStatus.queued))]
        for jid in queued:
            self._q.put(jid)

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=30)
            except queue.Empty:
                item = None
            if item is _STOP:
                return
            if isinstance(item, str):
                try:
                    self._process(item)
                except Exception as e:  # never let the worker die
                    log.error("job_crashed", job_id=item, error=repr(e))
                    self._finish(item, JobStatus.failed, f"internal error: {type(e).__name__}")
            if time.monotonic() - self._last_sweep > _SWEEP_EVERY_S:
                self._last_sweep = time.monotonic()
                try:
                    self.sweep_expired()
                except Exception as e:
                    log.error("sweep_failed", error=repr(e))

    def _set_progress(self, job_id: str, pct: int, msg: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is not None and job.status == JobStatus.running:
                job.progress = pct
                job.message = msg

    def _finish(self, job_id: str, status: JobStatus, msg: str | None) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job.status = status
            job.message = msg
            job.progress = 100 if status == JobStatus.done else job.progress
            job.finished_at = datetime.now(UTC)
            audit(
                s,
                actor="worker",
                action=f"job.{job.kind}",
                outcome="ok" if status == JobStatus.done else "error",
                document_id=job.document_id,
                job_id=job.id,
                detail=msg,
            )

    def _process(self, job_id: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.status != JobStatus.queued:
                return
            job.status = JobStatus.running
            job.started_at = datetime.now(UTC)
            kind, doc_id, params = job.kind, job.document_id, dict(job.params)
            doc = s.get(Document, doc_id)
            if doc is None or doc.status == DocumentStatus.deleted:
                job.status = JobStatus.cancelled
                job.message = "document no longer exists"
                return
            doc_kind = doc.kind

        log.info("job_start", job_id=job_id, kind=kind, document_id=doc_id)
        if kind == "parse":
            self._parse(job_id, doc_id, doc_kind)
        elif kind == "extract":
            self._extract(job_id, doc_id, doc_kind, params)
        else:
            self._finish(job_id, JobStatus.failed, f"unknown job kind {kind!r}")

    def _parse(self, job_id: str, doc_id: str, doc_kind: str) -> None:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is not None:
                doc.status = DocumentStatus.parsing
        path = upload_path(self._s, doc_id, doc_kind)
        try:
            if doc_kind == "xlsx":
                chunks = load_chunks(self._s, doc_id, doc_kind)  # validates it is readable
                page_count = None
                self._set_progress(job_id, 90, f"{len(chunks)} table chunks")
            else:
                self._set_progress(job_id, 10, "converting")
                result = docling_service.convert(path, self._s)
                docling_service.save_document(result.document, result_dir(self._s, doc_id))
                page_count = len(result.document.pages) or None
                self._set_progress(job_id, 90, "saved")
        except Exception as e:
            log.warning("parse_failed", document_id=doc_id, error=repr(e))
            with session_scope() as s:
                doc = s.get(Document, doc_id)
                if doc is not None:
                    doc.status = DocumentStatus.failed
                    doc.error = str(e)[:500]
            self._finish(job_id, JobStatus.failed, str(e)[:200])
            return
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is not None:
                doc.status = DocumentStatus.ready
                doc.parsed_at = datetime.now(UTC)
                if page_count:
                    doc.page_count = page_count
        self._finish(job_id, JobStatus.done, "ready")

    def _extract(self, job_id: str, doc_id: str, doc_kind: str, params: dict[str, object]) -> None:
        targets_raw = params.get("targets")
        targets = [FactType(t) for t in targets_raw] if isinstance(targets_raw, list) else None
        q = params.get("question")
        question = q if isinstance(q, str) else None
        fast = bool(params.get("fast", False))
        use_llm = bool(params.get("use_llm", True))

        try:
            chunks = load_chunks(self._s, doc_id, doc_kind)
        except Exception as e:  # reported to the user, never swallowed
            log.warning("chunking_failed", job=job_id, document=doc_id, error=str(e)[:200])
            self._finish(job_id, JobStatus.failed, f"could not read the document: {e}"[:200])
            return
        result = run_extraction(
            document_id=doc_id,
            chunks=chunks,
            settings=self._s,
            llm=self._llm if use_llm else None,
            targets=targets,
            question=question,
            fast=fast,
            progress=lambda pct, msg: self._set_progress(job_id, pct, msg),
        )
        out_dir = result_dir(self._s, doc_id) / "jobs"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        out = out_dir / f"{job_id}.json"
        out.write_text(result.model_dump_json())
        out.chmod(0o600)
        self._finish(
            job_id, JobStatus.done, f"{len(result.facts)} facts, {result.llm_calls} LLM calls"
        )

    # --- retention -----------------------------------------------------------------------------

    def sweep_expired(self) -> int:
        now = datetime.now(UTC)
        with session_scope() as s:
            expired = list(
                s.scalars(
                    select(Document).where(
                        Document.expires_at.is_not(None),
                        Document.expires_at < now,
                        Document.status != DocumentStatus.deleted,
                    )
                )
            )
            ids = [(d.id, d.kind) for d in expired]
        for doc_id, kind in ids:
            delete_document_files(self._s, doc_id, kind)
            with session_scope() as s:
                d = s.get(Document, doc_id)
                if d is not None:
                    d.status = DocumentStatus.deleted
                    d.display_name = "[deleted]"
                    for j in d.jobs:
                        if j.status in (JobStatus.queued, JobStatus.running):
                            j.status = JobStatus.cancelled
                audit(s, actor="worker", action="document.retention_delete", document_id=doc_id)
        if ids:
            log.info("retention_sweep", deleted=len(ids))
        return len(ids)


def delete_document_files(settings: Settings, doc_id: str, kind: str) -> None:
    p = upload_path(settings, doc_id, kind)
    if p.exists():
        _overwrite(p)
        p.unlink()
    rd = result_dir(settings, doc_id)
    if rd.exists():
        for f in rd.rglob("*"):
            if f.is_file():
                _overwrite(f)
        shutil.rmtree(rd, ignore_errors=True)


def _overwrite(path: Path) -> None:
    """Best-effort zeroing before unlink (real protection comes from full-disk encryption)."""
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(b"\0" * min(size, 64 * 1024 * 1024))
            fh.flush()
    except OSError:
        pass


def retention_expiry(settings: Settings) -> datetime | None:
    if settings.retention_days <= 0:
        return None
    return datetime.now(UTC) + timedelta(days=settings.retention_days)

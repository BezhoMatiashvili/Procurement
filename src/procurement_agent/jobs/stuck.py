"""The guard for a parse that never settled.

A parse job settles its job row and its document in one transaction (``Worker._settle``). If the
database fails at that moment - locked, or a full disk - the job is marked failed in a second
attempt that may fail as well, and the document is left ``parsing``: the page polls it every few
seconds for ever, and extraction answers 409 "not ready". Only a restart (``Worker._recover``)
used to clear it.

The periodic sweep runs on the worker's own thread, between jobs, so a job that is ``running``
while it runs is one that nothing is running any more. A document that is ``parsing`` with no
queued or running job to account for it, and has been for longer than a conversion may take
(``docling_timeout_s``) plus a margin, is the same thing seen from the other side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from procurement_agent.config import Settings
from procurement_agent.db import session_scope
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import audit, log

__all__ = ["STUCK_MARGIN_S", "fail_stuck_parses"]

#: Added to ``docling_timeout_s``: the time to save a parse and settle the job.
STUCK_MARGIN_S = 300.0
_MESSAGE = "could not be read: the reading did not finish"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _fail_document(s: Session, doc: Document) -> None:
    doc.status = DocumentStatus.failed
    doc.error = _MESSAGE
    audit(s, actor="worker", action="document.parse_stuck", outcome="error", document_id=doc.id)


def fail_stuck_parses(settings: Settings, *, running_too: bool, now: datetime | None = None) -> int:
    """Fail the parses nothing is running; returns how many documents were failed.

    ``running_too`` says the caller knows no job is being run now (the sweep, on the worker's
    thread, between jobs): a parse job still ``running`` is then failed as well.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.docling_timeout_s + STUCK_MARGIN_S)
    failed = 0
    with session_scope() as s:
        if running_too:
            for job in s.scalars(
                select(Job).where(Job.kind == "parse", Job.status == JobStatus.running)
            ):
                job.status = JobStatus.failed
                job.message = _MESSAGE[:200]
                job.finished_at = now
                if job.document.status == DocumentStatus.parsing:
                    _fail_document(s, job.document)
                    failed += 1
        for doc in s.scalars(select(Document).where(Document.status == DocumentStatus.parsing)):
            parses = sorted((j for j in doc.jobs if j.kind == "parse"), key=lambda j: j.created_at)
            if any(j.status in (JobStatus.queued, JobStatus.running) for j in parses):
                continue  # accounted for: waiting its turn, or being run
            last = parses[-1] if parses else None
            since = (last.finished_at or last.started_at or last.created_at) if last else None
            if _utc(since or doc.created_at) >= cutoff:
                continue
            _fail_document(s, doc)
            failed += 1
    if failed:
        log.warning("stuck_parses_failed", documents=failed)
    return failed

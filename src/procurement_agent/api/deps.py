"""Shared FastAPI dependencies / app state accessors, and the ownership checks both the JSON
API and the UI apply - kept here once so the two surfaces cannot drift apart."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from procurement_agent.config import Settings
from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.base import LLMClient
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.auth import Principal

#: What a 429 for a full job queue tells the client to wait. Jobs take seconds (a workbook) to
#: minutes (a PDF with the model), so this is a polite retry cadence, not a promise.
JOB_QUEUE_RETRY_AFTER_S = 30


def get_worker(request: Request) -> Worker:
    worker: Worker = request.app.state.worker
    return worker


def get_llm(request: Request) -> LLMClient | None:
    llm: LLMClient | None = request.app.state.llm
    return llm


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def owned_document(session: Session, principal: Principal, doc_id: str) -> Document:
    """Fetch a document the caller owns. Unknown and foreign ids both yield 404 (no enumeration)."""
    doc = session.get(Document, doc_id)
    if doc is None or doc.owner != principal.user or doc.status == DocumentStatus.deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


def owned_documents(session: Session, principal: Principal, *, limit: int) -> Sequence[Document]:
    """The caller's live documents, newest first."""
    return session.scalars(
        select(Document)
        .where(Document.owner == principal.user, Document.status != DocumentStatus.deleted)
        .order_by(Document.created_at.desc())
        .limit(limit)
    ).all()


def owned_job(
    session: Session, principal: Principal, job_id: str, *, live_document: bool = False
) -> Job:
    """Fetch a job the caller owns; 404 otherwise, as for documents.

    ``live_document`` is for anything that serves what the job *produced*: once its document
    is deleted the result is gone too, whatever may still be on disk.
    """
    job = session.get(Job, job_id) if len(job_id) <= 64 else None
    if job is None or job.owner != principal.user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if live_document and job.document.status == DocumentStatus.deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


def ensure_job_capacity(session: Session, settings: Settings, principal: Principal) -> None:
    """Refuse a new job while the caller already has ``max_active_jobs_per_user`` in flight.

    A soft cap - two requests racing may both pass - which is all it needs to be: it exists
    so one user's loop cannot queue hours of single-threaded work ahead of everyone else.
    """
    active = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.owner == principal.user,
            Job.status.in_((JobStatus.queued, JobStatus.running)),
        )
    )
    if (active or 0) >= settings.max_active_jobs_per_user:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many jobs in progress ({active}); wait for some to finish",
            headers={"Retry-After": str(JOB_QUEUE_RETRY_AFTER_S)},
        )

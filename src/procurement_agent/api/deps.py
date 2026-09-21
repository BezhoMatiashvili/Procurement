"""Shared FastAPI dependencies / app state accessors."""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.base import LLMClient
from procurement_agent.models import Document, DocumentStatus
from procurement_agent.security.auth import Principal


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

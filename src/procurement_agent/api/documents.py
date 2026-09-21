from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from procurement_agent.api.deps import client_ip, get_worker, owned_document
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.pipeline import upload_path
from procurement_agent.ingestion.render import render_page
from procurement_agent.jobs.worker import Worker, delete_document_files, retention_expiry
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import audit
from procurement_agent.security.auth import Principal, authenticate
from procurement_agent.security.upload_guard import UploadRejectedError, validate_upload

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


class DocumentOut(BaseModel):
    id: str
    name: str
    kind: str
    size_bytes: int
    page_count: int | None
    status: DocumentStatus
    error: str | None
    created_at: datetime
    expires_at: datetime | None


class UploadOut(BaseModel):
    document: DocumentOut
    parse_job_id: str


def _out(d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        name=d.display_name,
        kind=d.kind,
        size_bytes=d.size_bytes,
        page_count=d.page_count,
        status=d.status,
        error=d.error,
        created_at=d.created_at,
        expires_at=d.expires_at,
    )


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """Read the upload without ever buffering more than ``limit + 1`` bytes."""
    buf = bytearray()
    while True:
        part = await file.read(1024 * 1024)
        if not part:
            break
        buf.extend(part)
        if len(buf) > limit:
            raise UploadRejectedError(f"file too large; limit is {limit // (1024 * 1024)} MB")
    return bytes(buf)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=UploadOut)
async def upload_document(
    request: Request,
    file: UploadFile,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
) -> UploadOut:
    ip = client_ip(request)
    try:
        data = await _read_limited(file, settings.max_file_bytes)
        v = validate_upload(
            file.filename or "document",
            data,
            max_bytes=settings.max_file_bytes,
            max_pages=settings.max_pages,
            allowed_kinds=settings.allowed_kinds,
        )
    except UploadRejectedError as e:
        audit(
            session,
            actor=principal.user,
            action="document.upload",
            outcome="denied",
            ip=ip,
            detail=str(e),
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    doc = Document(
        owner=principal.user,
        display_name=v.display_name,
        kind=v.kind,
        size_bytes=v.size_bytes,
        sha256=hashlib.sha256(data).hexdigest(),
        page_count=v.page_count,
        expires_at=retention_expiry(settings),
    )
    session.add(doc)
    session.flush()
    target = upload_path(settings, doc.id, doc.kind)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(data)
    target.chmod(0o600)

    job = Job(document_id=doc.id, owner=principal.user, kind="parse")
    session.add(job)
    session.flush()
    audit(
        session,
        actor=principal.user,
        action="document.upload",
        document_id=doc.id,
        job_id=job.id,
        ip=ip,
    )
    session.commit()
    worker.submit(job.id)
    return UploadOut(document=_out(doc), parse_job_id=job.id)


@router.get("", response_model=list[DocumentOut])
def list_documents(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> list[DocumentOut]:
    docs = session.scalars(
        select(Document)
        .where(Document.owner == principal.user, Document.status != DocumentStatus.deleted)
        .order_by(Document.created_at.desc())
        .limit(200)
    )
    return [_out(d) for d in docs]


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(
    doc_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> DocumentOut:
    return _out(owned_document(session, principal, doc_id))


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    request: Request,
    doc_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    doc = owned_document(session, principal, doc_id)
    delete_document_files(settings, doc.id, doc.kind)
    doc.status = DocumentStatus.deleted
    doc.display_name = "[deleted]"
    for j in doc.jobs:
        if j.status in (JobStatus.queued, JobStatus.running):
            j.status = JobStatus.cancelled
    audit(
        session,
        actor=principal.user,
        action="document.delete",
        document_id=doc.id,
        ip=client_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{doc_id}/pages/{page_no}.png")
def page_image(
    doc_id: str,
    page_no: int,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    bbox: Annotated[
        list[str] | None, Query(description="l,t,r,b in PDF points (repeatable)")
    ] = None,
) -> Response:
    doc = owned_document(session, principal, doc_id)
    if doc.kind != "pdf":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "page images are only available for PDFs")
    boxes: list[tuple[float, float, float, float]] = []
    for raw in (bbox or [])[:20]:
        try:
            left, top, right, bottom = (float(x) for x in raw.split(","))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad bbox") from e
        boxes.append((left, top, right, bottom))
    try:
        png = render_page(upload_path(settings, doc.id, doc.kind), page_no, boxes)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

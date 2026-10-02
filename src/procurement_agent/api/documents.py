from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from procurement_agent.api.deps import (
    client_ip,
    ensure_job_capacity,
    get_worker,
    owned_document,
    owned_documents,
)
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.pipeline import upload_path
from procurement_agent.ingestion.render import MAX_BOXES, PageTooLargeError, render_page
from procurement_agent.jobs.retention import delete_document_files
from procurement_agent.jobs.worker import (
    Worker,
    mark_deleted,
    retention_expiry,
)
from procurement_agent.models import Document, DocumentStatus, Job
from procurement_agent.security.audit import audit, audit_now
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
    """Only reading the upload is async; everything after it runs on the threadpool.

    Validation opens the PDF with pdfium and walks the zip of an Office file, then the bytes
    are hashed, written and committed to SQLite - hundreds of milliseconds of blocking work on
    a large file, which on the event loop stalls every other request, including the progress
    polls of everyone else's jobs.
    """
    ip = client_ip(request)
    try:
        data = await _read_limited(file, settings.max_file_bytes)
    except UploadRejectedError as e:
        await run_in_threadpool(_deny, principal, ip, str(e))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return await run_in_threadpool(
        _store_upload, session, settings, worker, principal, file.filename or "document", data, ip
    )


def _deny(principal: Principal, ip: str | None, reason: str) -> None:
    """Record a refused upload in its own transaction; see ``audit_now``."""
    audit_now(
        actor=principal.user, action="document.upload", outcome="denied", ip=ip, detail=reason
    )


def _store_upload(
    session: Session,
    settings: Settings,
    worker: Worker,
    principal: Principal,
    filename: str,
    data: bytes,
    ip: str | None,
) -> UploadOut:
    ensure_job_capacity(session, settings, principal)
    try:
        v = validate_upload(
            filename,
            data,
            max_bytes=settings.max_file_bytes,
            max_pages=settings.max_pages,
            allowed_kinds=settings.allowed_kinds,
        )
    except UploadRejectedError as e:
        _deny(principal, ip, str(e))
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
    # The file goes down after the rows flushed (so the id exists) and before the commit (so
    # the worker never sees a row without its file). A commit that fails takes the file with
    # it: an orphan upload would be client data that no retention sweep knows about.
    target = upload_path(settings, doc.id, doc.kind)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        target.write_bytes(data)
        target.chmod(0o600)
        session.commit()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    worker.submit(job.id)
    return UploadOut(document=_out(doc), parse_job_id=job.id)


@router.get("", response_model=list[DocumentOut])
def list_documents(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> list[DocumentOut]:
    return [_out(d) for d in owned_documents(session, principal, limit=200)]


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
    """Commit the deletion, then remove the files - in that order; see ``jobs.worker``."""
    doc = owned_document(session, principal, doc_id)
    doc_id, kind = doc.id, doc.kind
    mark_deleted(doc)
    audit(
        session,
        actor=principal.user,
        action="document.delete",
        document_id=doc_id,
        ip=client_ip(request),
    )
    session.commit()
    delete_document_files(settings, doc_id, kind)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{doc_id}/pages/{page_no}.png")
def page_image(
    request: Request,
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
    for raw in (bbox or [])[:MAX_BOXES]:
        try:
            left, top, right, bottom = (float(x) for x in raw.split(","))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad bbox") from e
        # float() reads "nan" and "inf"; neither is a place on a page
        if not all(math.isfinite(v) for v in (left, top, right, bottom)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad bbox")
        boxes.append((left, top, right, bottom))
    try:
        png = render_page(upload_path(settings, doc.id, doc.kind), page_no, boxes)
    except PageTooLargeError as e:
        # the PDF's own page size: nothing the caller can change, and not "not found" either
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    # A rendered page is document content, so reading it is audited like reading a result.
    audit(
        session,
        actor=principal.user,
        action="document.page_view",
        document_id=doc.id,
        ip=client_ip(request),
        detail=f"page {page_no}",
    )
    # no-store rather than a short max-age: a cached page image outlives logout and deletion.
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})

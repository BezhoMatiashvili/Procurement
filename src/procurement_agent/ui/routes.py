"""Minimal server-rendered UI. No JavaScript frameworks, no CDN — everything is served locally."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from procurement_agent.api import documents as docs_api
from procurement_agent.api.deps import get_worker, owned_document
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.pipeline import result_dir
from procurement_agent.extraction.schemas import DEFAULT_TARGETS, ExtractionResult, FactType
from procurement_agent.jobs.worker import Worker
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import audit
from procurement_agent.security.auth import COOKIE_NAME, Principal, _lookup, authenticate

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)
_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(default=True, default_for_string=True),
    undefined=StrictUndefined,
)
templates = Jinja2Templates(env=_env)


def _render(request: Request, name: str, **ctx: object) -> Response:
    resp: Response = templates.TemplateResponse(request, name, ctx)
    return resp


@router.get("/")
def root(request: Request) -> Response:
    return RedirectResponse("/ui/documents", status_code=status.HTTP_302_FOUND)


# --- login -------------------------------------------------------------------------------------


def _safe_next(target: str | None) -> str:
    """Only ever redirect back to a path inside this UI.

    Anything else (absolute URL, protocol-relative ``//evil.com``, backslash trickery) falls
    back to the document list, so ``?next=`` can never become an open redirect.
    """
    if not target or not target.startswith("/ui/"):
        return "/ui/documents"
    if target.startswith("//") or "\\" in target or target.startswith("/ui/login"):
        return "/ui/documents"
    return target


@router.get("/login")
def login_form(
    request: Request, next_: Annotated[str | None, Query(alias="next")] = None
) -> Response:
    return _render(request, "login.html", error=None, next=_safe_next(next_))


@router.post("/login")
def login(
    request: Request,
    api_key: Annotated[str, Form(max_length=200)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    next_: Annotated[str | None, Form(max_length=500, alias="next")] = None,
) -> Response:
    dev_ok = (
        settings.env == "dev"
        and bool(settings.dev_api_key)
        and hmac.compare_digest(api_key, settings.dev_api_key or "")
    )
    row = None if dev_ok else _lookup(session, api_key)
    if not dev_ok and row is None:
        audit(session, actor="anonymous", action="ui.login", outcome="denied")
        return _render(request, "login.html", error="Invalid API key", next=_safe_next(next_))
    resp = RedirectResponse(_safe_next(next_), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        COOKIE_NAME,
        api_key,
        httponly=True,
        samesite="strict",
        secure=settings.env == "prod",
        max_age=8 * 3600,
        path="/",
    )
    audit(session, actor="dev" if dev_ok else (row.user if row else "?"), action="ui.login")
    return resp


@router.post("/logout")
def logout(_: Annotated[Principal, Depends(authenticate)]) -> Response:
    resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- documents ---------------------------------------------------------------------------------


@router.get("/documents")
def documents_page(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    docs = session.scalars(
        select(Document)
        .where(Document.owner == principal.user, Document.status != DocumentStatus.deleted)
        .order_by(Document.created_at.desc())
        .limit(100)
    ).all()
    return _render(request, "documents.html", user=principal.user, documents=docs)


@router.post("/documents/upload")
async def upload(
    request: Request,
    file: UploadFile,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
) -> Response:
    try:
        out = await docs_api.upload_document(request, file, principal, session, settings, worker)
    except HTTPException as e:
        return _render(request, "error.html", user=principal.user, message=e.detail)
    return RedirectResponse(
        f"/ui/documents/{out.document.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/documents/{doc_id}")
def document_page(
    request: Request,
    doc_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    doc = owned_document(session, principal, doc_id)
    jobs = sorted(doc.jobs, key=lambda j: j.created_at, reverse=True)
    targets = [(t.value, LABELS[t][0], LABELS[t][1]) for t in DEFAULT_TARGETS]
    return _render(
        request, "document.html", user=principal.user, doc=doc, jobs=jobs, targets=targets
    )


@router.post("/documents/{doc_id}/delete")
def delete(
    request: Request,
    doc_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    docs_api.delete_document(request, doc_id, principal, session, settings)
    return RedirectResponse("/ui/documents", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/documents/{doc_id}/extract")
def extract(
    request: Request,
    doc_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    worker: Annotated[Worker, Depends(get_worker)],
    targets: Annotated[list[str] | None, Form()] = None,
    question: Annotated[str | None, Form(max_length=500)] = None,
    fast: Annotated[bool, Form()] = False,
    use_llm: Annotated[bool, Form()] = True,
) -> Response:
    from procurement_agent.api.extraction import ExtractRequest, start_extraction

    valid = [FactType(t) for t in (targets or []) if t in FactType.__members__]
    body = ExtractRequest(
        targets=valid or None,
        question=(question or "").strip() or None,
        fast=fast,
        use_llm=use_llm,
    )
    job = start_extraction(request, doc_id, body, principal, session, worker)
    return RedirectResponse(f"/ui/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


# --- jobs / results ----------------------------------------------------------------------------


@router.get("/jobs/{job_id}")
def job_page(
    request: Request,
    job_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    job = session.get(Job, job_id)
    if job is None or job.owner != principal.user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    result: ExtractionResult | None = None
    if job.kind == "extract" and job.status == JobStatus.done:
        path = result_dir(settings, job.document_id) / "jobs" / f"{job.id}.json"
        if path.exists():
            result = ExtractionResult.model_validate_json(path.read_text())
    return _render(
        request, "job.html", user=principal.user, job=job, doc=job.document, result=result
    )

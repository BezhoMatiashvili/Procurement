from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from procurement_agent.api.deps import (
    client_ip,
    ensure_job_capacity,
    get_worker,
    owned_document,
    owned_job,
)
from procurement_agent.api.refusals import require_readable
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.export import to_xlsx
from procurement_agent.extraction.pipeline import result_dir
from procurement_agent.extraction.schemas import ExtractionResult, FactType
from procurement_agent.jobs.worker import Worker
from procurement_agent.models import DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import audit
from procurement_agent.security.auth import Principal, authenticate

router = APIRouter(prefix="/api/v1", tags=["extraction"])


class ExtractRequest(BaseModel):
    targets: list[FactType] | None = Field(default=None, max_length=30)
    question: str | None = Field(default=None, min_length=3, max_length=500)
    fast: bool = False
    #: Off unless asked for. Measured on the client's documents the model pass lowers precision
    #: (84% rules-only against 66% with gemma3:4b) and costs minutes per document; it labelled a
    #: cadastral code a contract number and a bank block payment terms. A question can only be
    #: answered by the model, so asking one turns it on.
    use_llm: bool = False


class JobOut(BaseModel):
    id: str
    document_id: str
    kind: str
    status: JobStatus
    progress: int
    message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    #: the job is done, but the model that was asked did not answer: the result is the rules'
    #: alone (``LLM_UNAVAILABLE`` in its message and its warnings)
    degraded: bool = False


def _job_out(j: Job) -> JobOut:
    return JobOut(
        id=j.id,
        document_id=j.document_id,
        kind=j.kind,
        status=j.status,
        progress=j.progress,
        message=j.message,
        created_at=j.created_at,
        started_at=j.started_at,
        finished_at=j.finished_at,
        degraded=bool(j.params.get("degraded")),
    )


@router.post(
    "/documents/{doc_id}/extract", status_code=status.HTTP_202_ACCEPTED, response_model=JobOut
)
def start_extraction(
    request: Request,
    doc_id: str,
    body: ExtractRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    worker: Annotated[Worker, Depends(get_worker)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobOut:
    doc = owned_document(session, principal, doc_id)
    if doc.status != DocumentStatus.ready:
        msg = f"document is not ready (status: {doc.status.value})"
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    require_readable(settings, doc)  # ready, but holding no text: 409 NO_TEXT
    ensure_job_capacity(session, settings, principal)
    params: dict[str, object] = {"fast": body.fast, "use_llm": body.use_llm or bool(body.question)}
    if body.targets:
        params["targets"] = [t.value for t in body.targets]
    if body.question:
        params["question"] = body.question.strip()
    job = Job(document_id=doc.id, owner=principal.user, kind="extract", params=params)
    session.add(job)
    session.flush()
    audit(
        session,
        actor=principal.user,
        action="extract.start",
        document_id=doc.id,
        job_id=job.id,
        ip=client_ip(request),
    )
    session.commit()
    worker.submit(job.id)
    return _job_out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> JobOut:
    return _job_out(owned_job(session, principal, job_id))


@router.get("/jobs/{job_id}/result")
def get_result(
    request: Request,
    job_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    fmt: Annotated[Literal["json", "xlsx"], Query(alias="format")] = "json",
) -> Response:
    job = owned_job(session, principal, job_id, live_document=True)
    if job.kind != "extract":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job has no result")
    if job.status != JobStatus.done:
        msg = f"job is {job.status.value}"
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    path = result_dir(settings, job.document_id) / "jobs" / f"{job.id}.json"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "result no longer available")
    raw = path.read_text()
    audit(
        session,
        actor=principal.user,
        action=f"result.read.{fmt}",
        document_id=job.document_id,
        job_id=job.id,
        ip=client_ip(request),
    )
    if fmt == "json":
        return Response(raw, media_type="application/json")
    result = ExtractionResult.model_validate_json(raw)
    data = to_xlsx(result, job.document.display_name)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="extraction-{job.id[:8]}.xlsx"'},
    )

from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from procurement_agent.api.deps import get_llm
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_engine, get_session
from procurement_agent.llm.base import LLMClient
from procurement_agent.models import Job, JobStatus
from procurement_agent.security.auth import Principal, authenticate

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only. Deliberately reveals nothing about configuration."""
    return {"status": "ok"}


@router.get("/ready", response_model=None)
def ready(request: Request) -> dict[str, str] | JSONResponse:
    """Readiness: the database answers and the worker thread is alive and not stuck.

    Unauthenticated like ``/health``, so it says only *which* part is down, never how it is
    configured. ``/health`` stays pure liveness: a restart cannot fix a slow disk.

    ``scripts/supervise.py`` restarts the server after a run of consecutive failures of THIS
    endpoint (an answer of 503, or none), which loses the job the worker is running. That is why
    a busy worker counts as stalled only after its longest legitimate step plus two minutes, and
    why the answer must not depend on anything slower than that. It is a sync route, so it runs on
    the thread pool: a pool held by other work makes it answer late, which the supervisor
    cannot tell from a hang.
    """
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return _unready("database")
    worker = request.app.state.worker
    if worker is None or not worker.is_alive:
        return _unready("worker_down")
    if not worker.healthy():
        return _unready("worker_stalled")
    return {"status": "ready"}


def _unready(reason: str) -> JSONResponse:
    return JSONResponse({"status": "unavailable", "reason": reason}, status_code=503)


@router.get("/api/v1/status")
def status_(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    llm: Annotated[LLMClient | None, Depends(get_llm)],
) -> dict[str, object]:
    # How much work is queued server-wide says what the other users are doing, so it is not shown:
    # a caller sees its own jobs. The model probe is answered from a ten-second cache (see
    # ``OllamaClient.health``), so polling this does not make the server poll the model.
    mine = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.owner == principal.user,
            Job.status.in_((JobStatus.queued, JobStatus.running)),
        )
    )
    return {
        "env": settings.env,
        "llm": llm.health() if llm is not None else {"ok": False, "error": "disabled"},
        "model": settings.llm_model,
        "fast_model": settings.llm_fast_model,
        "ocr": shutil.which("tesseract") is not None,
        "ocr_langs": list(settings.ocr_langs),
        "my_active_jobs": int(mine or 0),
        "limits": {"max_file_mb": settings.max_file_mb, "max_pages": settings.max_pages},
        "retention_days": settings.retention_days,
    }

from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from procurement_agent.api.deps import get_llm, get_worker
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_engine
from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.base import LLMClient
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
    configured. ``/health`` stays pure liveness: a restart cannot fix a slow disk, and an
    orchestrator that restarts on readiness would kill the job the worker is busy with.
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
    _: Annotated[Principal, Depends(authenticate)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
    llm: Annotated[LLMClient | None, Depends(get_llm)],
) -> dict[str, object]:
    return {
        "env": settings.env,
        "llm": llm.health() if llm is not None else {"ok": False, "error": "disabled"},
        "model": settings.llm_model,
        "fast_model": settings.llm_fast_model,
        "ocr": shutil.which("tesseract") is not None,
        "ocr_langs": list(settings.ocr_langs),
        "queue": worker.queue_size,
        "limits": {"max_file_mb": settings.max_file_mb, "max_pages": settings.max_pages},
        "retention_days": settings.retention_days,
    }

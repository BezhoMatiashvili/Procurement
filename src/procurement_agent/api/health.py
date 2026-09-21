from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from procurement_agent.api.deps import get_llm, get_worker
from procurement_agent.config import Settings, get_settings
from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.base import LLMClient
from procurement_agent.security.auth import Principal, authenticate

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only. Deliberately reveals nothing about configuration."""
    return {"status": "ok"}


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

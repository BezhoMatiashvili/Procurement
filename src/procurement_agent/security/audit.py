"""Structured logging + append-only audit trail.

Rules:
  * Log records never carry document text, extracted values, file names chosen by users
    (only our UUIDs), or API keys.
  * ``audit()`` writes to the DB table *and* to the structured log so that the trail survives
    even if the database is lost.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from sqlalchemy.orm import Session

from procurement_agent.db import session_scope
from procurement_agent.models import AuditLog

_SECRET_KEYS = {"api_key", "x-api-key", "authorization", "key", "password", "token", "secret"}
#: API keys (``pa_``) and UI session tokens (``ps_``) - neither may ever reach a log line.
_KEY_PATTERN = re.compile(r"p[as]_[A-Za-z0-9_\-]{20,}")


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k in list(event_dict):
        if k.lower() in _SECRET_KEYS:
            event_dict[k] = "[redacted]"
    for k, v in event_dict.items():
        if isinstance(v, str) and _KEY_PATTERN.search(v):
            event_dict[k] = _KEY_PATTERN.sub("[redacted]", v)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)


log = structlog.get_logger("procurement_agent")


def audit(
    session: Session,
    *,
    actor: str,
    action: str,
    outcome: str = "ok",
    document_id: str | None = None,
    job_id: str | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    detail = detail[:255] if detail else None
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            outcome=outcome,
            document_id=document_id,
            job_id=job_id,
            ip=ip,
            detail=detail,
        )
    )
    log.info(
        "audit",
        actor=actor,
        action=action,
        outcome=outcome,
        document_id=document_id,
        job_id=job_id,
        ip=ip,
        detail=detail,
    )


def audit_now(
    *,
    actor: str,
    action: str,
    outcome: str = "ok",
    document_id: str | None = None,
    job_id: str | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Write one audit row in its own transaction, committed before this returns.

    For denials: the request's session is rolled back when the ``HTTPException`` that follows
    propagates, and an ``audit()`` added to it goes down with it - which is how refused uploads
    used to leave no trace. The caller must not hold an uncommitted write on another session
    (SQLite allows one writer; this would wait on it).
    """
    with session_scope() as s:
        audit(
            s,
            actor=actor,
            action=action,
            outcome=outcome,
            document_id=document_id,
            job_id=job_id,
            ip=ip,
            detail=detail,
        )

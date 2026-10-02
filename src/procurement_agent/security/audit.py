"""Structured logging + append-only audit trail.

Rules:
  * Log records never carry document text, extracted values, file names chosen by users
    (only our UUIDs), or API keys.
  * ``audit()`` writes to the DB table *and* to the structured log so that the trail survives
    even if the database is lost.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
import time
from typing import Any

import structlog
from sqlalchemy.orm import Session

from procurement_agent.db import session_scope
from procurement_agent.models import AuditLog
from procurement_agent.security.logscrub import content_free, install_stdlib_scrub

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
        content_free,
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
    install_stdlib_scrub()


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


# --- blocked outbound connections ----------------------------------------------------------------

#: One audit row per (kind, destination) per window, and at most this many rows per window: a
#: document with ten thousand links must not turn the audit table into what fills the disk.
_EGRESS_WINDOW_S = 60.0
_EGRESS_ROWS_PER_WINDOW = 30
_EGRESS_MAX_TRACKED = 512
_egress_lock = threading.Lock()
_egress_seen: dict[tuple[str, str], float] = {}
_egress_window = [0.0, 0]  # [start of the window, rows written in it]
_NOT_A_HOST = re.compile(r"[^A-Za-z0-9.:\-\[\]%_]")


def report_blocked_egress(kind: str, destination: str) -> None:
    """What the egress guard calls when it refuses a connection or a lookup.

    The destination is a host name or an address - what the process tried to reach, which is what
    an operator needs to see, and never document text. It can still be chosen by a document's
    author (a link in a DOCX), so it is cut to a plain host-shaped string before it reaches the
    log or a row. The row is written from a short-lived thread: the caller may be inside a
    database transaction, and the audit write waits on SQLite's one writer.
    """
    shown = _NOT_A_HOST.sub("?", destination)[:100]
    kind = _NOT_A_HOST.sub("?", kind)[:20]
    log.warning("egress_blocked", kind=kind, destination=shown)
    now = time.monotonic()
    with _egress_lock:
        if now - _egress_window[0] > _EGRESS_WINDOW_S:
            _egress_window[:] = [now, 0]
            _egress_seen.clear()
        if (kind, shown) in _egress_seen or _egress_window[1] >= _EGRESS_ROWS_PER_WINDOW:
            return
        if len(_egress_seen) >= _EGRESS_MAX_TRACKED:
            return
        _egress_seen[(kind, shown)] = now
        _egress_window[1] += 1
    threading.Thread(target=_audit_egress, args=(kind, shown), daemon=True).start()


def _audit_egress(kind: str, destination: str) -> None:
    with contextlib.suppress(Exception):  # no database yet, or a full disk: the log line stands
        audit_now(
            actor="system",
            action="egress.blocked",
            outcome="denied",
            detail=f"{kind} {destination}",
        )


# --- refused lookups -----------------------------------------------------------------------------

_ID_SHAPE = re.compile(r"[0-9a-f]{1,32}")


def audit_denied_access(actor: str, kind: str, ref: str) -> None:
    """Record that ``actor`` asked for a document or job that is not theirs (or does not exist).

    Both look the same from outside - the answer is a 404 either way, so ids cannot be enumerated
    - but a run of them is how someone probing for other people's documents shows up. The id is
    kept only if it has the shape of one of ours; anything else is whatever the caller typed.
    Never raises: a failure to record must not turn a 404 into a 500.
    """
    from procurement_agent.security.context import request_ip

    shown = ref if _ID_SHAPE.fullmatch(ref) else "malformed id"
    with contextlib.suppress(Exception):
        audit_now(
            actor=actor,
            action="access.denied",
            outcome="denied",
            ip=request_ip.get(),
            detail=f"{kind} {shown}",
        )

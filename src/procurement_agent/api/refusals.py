"""Refusing to build a check on a document that was not read, in a form a client can act on.

A refusal carries a stable code, the same sentence in English and in Georgian, and - where it
is about one document - that document's id, which is ours and never a name the user gave the
file. The page shows the sentence in the page's language (``ui.routes._http_error``); an API
client reads ``detail.code``.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from procurement_agent.config import Settings
from procurement_agent.ingestion import read_quality as rq
from procurement_agent.ingestion.read_quality import ReadQuality, StoredParseError
from procurement_agent.jobs.stuck import fail_unreadable
from procurement_agent.models import Document, DocumentStatus
from procurement_agent.security.audit import AuditUnavailableError, audit_committed
from procurement_agent.ui.i18n import STRINGS

__all__ = ["audit_or_503", "refusal", "require_readable", "unreadable"]


def refusal(
    code: str,
    http_status: int = status.HTTP_409_CONFLICT,
    *,
    document_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """A structured, content-free refusal: ``detail`` is ``{code, message_en, message_ka}``.

    The messages are the interface's own strings (``ui.i18n``), so the API and the page say the
    same thing; ``document_id`` is ours, never a name the user gave the file.
    """
    en, ka = STRINGS[f"error.{code}"]
    detail: dict[str, str] = {"code": code, "message_en": en, "message_ka": ka}
    if document_id is not None:
        detail["document_id"] = document_id
    return HTTPException(http_status, detail, headers=headers)


def unreadable(e: StoredParseError) -> HTTPException:
    """The refusal for a document whose stored reading is gone or damaged: it is marked failed
    (so nothing offers it again) and the user is told to upload it again."""
    if e.document_id is not None:
        fail_unreadable(e.document_id)
    return refusal(rq.DOCUMENT_UNREADABLE, document_id=e.document_id)


def require_readable(settings: Settings, doc: Document) -> ReadQuality:
    """The quality of a ready document, or the refusal to build a check on it.

    A document that is not ready is refused as it always was (409, with its status). One that is
    ready but holds no text - parsed before a parse could refuse that - is refused with
    ``NO_TEXT``.
    """
    if doc.status != DocumentStatus.ready:
        msg = f"document {doc.id} is {doc.status.value}, not ready"
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    try:
        quality = rq.read_quality(settings, doc)
    except StoredParseError as e:
        raise unreadable(e) from None
    if not quality.has_text:
        raise refusal(rq.NO_TEXT, document_id=doc.id)
    return quality


#: What a 503 for a lost audit row tells the client to wait.
_AUDIT_RETRY_AFTER_S = "5"


def audit_or_503(
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
    """Record that a report or a read is about to be served, committed before it is.

    When the record cannot be stored the request fails with a structured 503
    (``AUDIT_UNAVAILABLE``, in both languages) and serves nothing: a read of client documents
    that left no trace is not something to hand over. Nothing about the report is in the refusal.
    """
    try:
        audit_committed(
            session,
            actor=actor,
            action=action,
            outcome=outcome,
            document_id=document_id,
            job_id=job_id,
            ip=ip,
            detail=detail,
        )
    except AuditUnavailableError:
        raise refusal(
            "AUDIT_UNAVAILABLE",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": _AUDIT_RETRY_AFTER_S},
        ) from None

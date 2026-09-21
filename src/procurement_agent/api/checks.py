"""Document checks: acceptance act (AOA) against its contract.

Synchronous on purpose. The whole check is deterministic and runs in milliseconds — unlike
extraction, which queues a background job because it may call the LLM.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from procurement_agent.api.deps import client_ip, owned_document
from procurement_agent.checks.aoa import ContractorType, check_aoa
from procurement_agent.checks.findings import CheckReport
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.pipeline import load_chunks, upload_path
from procurement_agent.models import Document, DocumentStatus
from procurement_agent.registry.snapshot import open_snapshot
from procurement_agent.report.docx_annotate import UnsupportedDocumentError, annotate_docx
from procurement_agent.security.audit import audit
from procurement_agent.security.auth import Principal, authenticate

router = APIRouter(prefix="/api/v1/checks", tags=["checks"])

_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class AoaCheckRequest(BaseModel):
    aoa_document_id: str = Field(max_length=64)
    contract_document_id: str | None = Field(default=None, max_length=64)
    #: Drives the NET/VAT wording rules. Nothing in the document states it, so when it is
    #: omitted that rule is reported as skipped rather than guessed.
    contractor_type: ContractorType | None = None


def _document_text(settings: Settings, doc: Document) -> str:
    if doc.status != DocumentStatus.ready:
        msg = f"document {doc.id} is {doc.status.value}, not ready"
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    chunks = load_chunks(settings, doc.id, doc.kind)
    return "\n".join(c.body or c.text for c in chunks)


@router.post("/aoa")
def check_acceptance_act(
    request: Request,
    body: AoaCheckRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    fmt: Annotated[Literal["json", "docx"], Query(alias="format")] = "json",
) -> Response:
    aoa = owned_document(session, principal, body.aoa_document_id)
    contract = (
        owned_document(session, principal, body.contract_document_id)
        if body.contract_document_id
        else None
    )
    registry = open_snapshot(settings.registry_db) if settings.registry_db else None
    try:
        report = check_aoa(
            aoa_text=_document_text(settings, aoa),
            aoa_id=aoa.id,
            contract_text=_document_text(settings, contract) if contract else None,
            contract_id=contract.id if contract else "contract",
            registry=registry,
            contractor_type=body.contractor_type,
        )
    finally:
        if registry is not None:
            registry.close()

    audit(
        session,
        actor=principal.user,
        action=f"check.aoa.{fmt}",
        document_id=aoa.id,
        ip=client_ip(request),
        detail=f"{len(report.errors)} error(s), {len(report.findings)} finding(s)",
    )
    if fmt == "json":
        return Response(report.model_dump_json(), media_type="application/json")
    return _annotated_docx(settings, aoa, report)


def _annotated_docx(settings: Settings, aoa: Document, report: CheckReport) -> Response:
    """A copy of the act with one Word comment per finding, which is what the client asked for."""
    source = upload_path(settings, aoa.id, aoa.kind)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "annotated.docx"
        try:
            annotate_docx(source, report.findings, out)
        except UnsupportedDocumentError as e:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e)) from e
        data = out.read_bytes()
    return Response(
        data,
        media_type=_DOCX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="checked-{aoa.id[:8]}.docx"',
            "X-Finding-Count": str(len(report.findings)),
            "X-Error-Count": str(len(report.errors)),
        },
    )


@router.get("/contractor-types")
def contractor_types(
    _: Annotated[Principal, Depends(authenticate)],
) -> dict[str, list[str]]:
    """The values ``contractor_type`` accepts, so a client need not hard-code them."""
    return {"contractor_types": list(get_args(ContractorType))}

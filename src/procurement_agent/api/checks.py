"""Document checks: acceptance act against its contract, BOQ bids, and two documents compared.

Synchronous on purpose. Every check here is deterministic and runs in milliseconds — unlike
extraction, which queues a background job because it may call the LLM. The comparison and the
act check keep that property: each optional model pass is a separate job, and the check
endpoints only ever *read* what that job stored.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from procurement_agent.api.deps import (
    client_ip,
    ensure_job_capacity,
    get_llm,
    get_worker,
    owned_document,
)
from procurement_agent.api.extraction import JobOut, _job_out
from procurement_agent.checks.aoa import ContractorType, check_aoa, scope_review_items
from procurement_agent.checks.aoa_llm import ScopeReviewSet, review_digest
from procurement_agent.checks.aoa_spelling import open_speller
from procurement_agent.checks.boq import (
    BidComparison,
    BoqLine,
    BoqWorkbook,
    LineDiff,
    cents,
    compare_all,
    rate_outliers,
    read_bid,
    read_boq,
    template_findings,
    write_merged_workbook,
)
from procurement_agent.checks.compare import ComparisonReport, DocumentTooLargeError
from procurement_agent.checks.compare_llm import ExplanationSet, apply_explanations
from procurement_agent.checks.findings import CheckReport, Finding, SkipNote
from procurement_agent.checks.fx_invoice import AdvanceInput, to_money
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.pipeline import (
    StoredDocument,
    compare_stored,
    is_scanned_pdf,
    load_document_text,
    result_dir,
    upload_path,
)
from procurement_agent.fx.provider import open_rate_provider
from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.base import LLMClient
from procurement_agent.models import Document, DocumentStatus, Job
from procurement_agent.registry.snapshot import open_snapshot
from procurement_agent.report.compare_xlsx import to_xlsx as compare_to_xlsx
from procurement_agent.report.docx_annotate import UnsupportedDocumentError, annotate_docx
from procurement_agent.security.audit import audit
from procurement_agent.security.auth import Principal, authenticate

router = APIRouter(prefix="/api/v1/checks", tags=["checks"])

_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class AoaCheckRequest(BaseModel):
    aoa_document_id: str = Field(max_length=64)
    contract_document_id: str | None = Field(default=None, max_length=64)
    #: Drives the NET/VAT wording rules. Nothing in the document states it, so when it is
    #: omitted that rule is reported as skipped rather than guessed.
    contractor_type: ContractorType | None = None
    #: An advance paid on a foreign-currency act, in the act's currency, and the day it was
    #: paid (rule 9: the advance is invoiced at that day's rate). Omitted, each is read from the
    #: act; an amount of 0 says no advance was paid.
    advance_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    advance_date: date | None = Field(default=None, ge=date(1990, 1, 1), le=date(2100, 12, 31))
    #: A finished ``aoa_review`` job (``POST /aoa/review``) whose model answers are applied to
    #: the notes the rules could not decide. The check itself never calls the model.
    review_job_id: str | None = Field(default=None, max_length=64)

    def advance(self) -> AdvanceInput | None:
        if self.advance_amount is None and self.advance_date is None:
            return None
        amount = None if self.advance_amount is None else to_money(self.advance_amount)
        return AdvanceInput(amount=amount, paid_on=self.advance_date)


def _document_text(settings: Settings, doc: Document) -> str:
    if doc.status != DocumentStatus.ready:
        msg = f"document {doc.id} is {doc.status.value}, not ready"
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    return load_document_text(settings, doc.id, doc.kind)


def run_aoa(
    settings: Settings,
    aoa: Document,
    contract: Document | None,
    contractor_type: ContractorType | None,
    advance: AdvanceInput | None = None,
    *,
    review_job_id: str | None = None,
) -> CheckReport:
    """The whole act check. Shared by the JSON API, the DOCX download and the UI page, so the
    registry snapshot and the exchange-rate source are opened and closed in exactly one place.

    ``review_job_id`` names a model review to apply, already resolved through
    ``owned_review_job``; its answers are checked again against the notes this run leaves open.
    """
    review = (
        load_review(settings, aoa.id, review_job_id)
        if review_job_id and contract is not None
        else None
    )
    registry = open_snapshot(settings.registry_db) if settings.registry_db else None
    try:
        rates = open_rate_provider(settings)
        try:
            return check_aoa(
                aoa_text=_document_text(settings, aoa),
                aoa_id=aoa.id,
                contract_text=_document_text(settings, contract) if contract else None,
                contract_id=contract.id if contract else "contract",
                registry=registry,
                contractor_type=contractor_type,
                rates=rates,
                advance=advance,
                review=review,
                # a scan's letters and codes may be misread: its differences are said so
                aoa_ocr=is_scanned_pdf(settings, aoa.id, aoa.kind),
                contract_ocr=(
                    contract is not None and is_scanned_pdf(settings, contract.id, contract.kind)
                ),
                speller=open_speller(settings),
            )
        finally:
            rates.close()
    finally:
        if registry is not None:
            registry.close()


# --- the act check's model review ------------------------------------------------------------

_REVIEW_KIND = "aoa_review"


def owned_review_job(
    session: Session,
    principal: Principal,
    job_id: str | None,
    aoa: Document,
    contract: Document | None,
) -> Job | None:
    """The caller's review job for exactly this act and contract, or None - never an error.

    The same guard as ``owned_explain_job``, for the same reason: the id arrives in a URL and
    is joined into a file path, so its shape is checked before anything touches the disk, and
    its owner, kind and pair before its file is read. An id that fails is ignored, like one
    whose job never finished - the rules' report stands on its own.
    """
    if contract is None:
        return None
    if not job_id or len(job_id) > 64 or not (job_id.isascii() and job_id.isalnum()):
        return None
    job = session.get(Job, job_id)
    if (
        job is None
        or job.owner != principal.user
        or job.kind != _REVIEW_KIND
        or job.document_id != aoa.id
        or job.params.get("contract") != contract.id
    ):
        return None
    return job


def load_review(settings: Settings, doc_id: str, job_id: str) -> ScopeReviewSet | None:
    """A stored review, or None when there is nothing readable there. Callers resolve ``job_id``
    through ``owned_review_job`` first; the shape check here is a second lock on the path."""
    if not (job_id.isascii() and job_id.isalnum()):
        return None
    path = result_dir(settings, doc_id) / "jobs" / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return ScopeReviewSet.model_validate_json(path.read_text())
    except ValueError:
        return None  # a job of another kind wrote here; not a review


@dataclass(frozen=True)
class QueuedReview:
    """What asking for a review came to: its job - just queued, or the earlier one that asked
    the same thing - or none when the rules left nothing open; and how many notes it asks."""

    job: Job | None
    items: int
    created: bool = False


def queue_aoa_review(
    request: Request,
    session: Session,
    settings: Settings,
    principal: Principal,
    worker: Worker,
    llm: LLMClient | None,
    aoa: Document,
    contract: Document,
) -> QueuedReview:
    """Queue the model review of an act's open notes, or find the one that already asked.

    Shared by the report's button and the API. Nothing is queued when the operator turned the
    review off, when no model is configured, or when the rules left nothing open. A review of
    the same notes by the same model and prompt - the digest - is reused whatever state it
    ended in, a failure included: asking again would bring the same answer, or the same
    failure, after another minute or two of the single worker's time.
    """
    if not settings.aoa_llm_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "the model review is disabled on this server")
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no LLM backend is configured")
    items = scope_review_items(
        aoa_text=_document_text(settings, aoa), contract_text=_document_text(settings, contract)
    )
    if not items:
        return QueuedReview(None, 0)
    digest = review_digest(items, model=settings.llm_fast_model)
    earlier = session.scalars(
        select(Job)
        .where(Job.owner == principal.user, Job.kind == _REVIEW_KIND, Job.document_id == aoa.id)
        .order_by(Job.created_at.desc())
    )
    for job in earlier:
        if job.params.get("contract") == contract.id and job.params.get("digest") == digest:
            return QueuedReview(job, len(items))

    ensure_job_capacity(session, settings, principal)
    job = Job(
        document_id=aoa.id,
        owner=principal.user,
        kind=_REVIEW_KIND,
        params={"contract": contract.id, "digest": digest},
    )
    session.add(job)
    session.flush()
    audit(
        session,
        actor=principal.user,
        action="check.aoa.review",
        document_id=aoa.id,
        job_id=job.id,
        ip=client_ip(request),
        detail=f"{len(items)} note(s)",
    )
    session.commit()
    worker.submit(job.id)
    return QueuedReview(job, len(items), created=True)


class AoaReviewRequest(BaseModel):
    aoa_document_id: str = Field(max_length=64)
    contract_document_id: str = Field(max_length=64)


class AoaReviewOut(BaseModel):
    """The review of an act's open notes. ``job`` is None when the rules left nothing open;
    pass its id as ``review_job_id`` to ``POST /aoa`` once it is done."""

    job: JobOut | None = None
    #: the notes the model is asked about
    items: int = 0


@router.post("/aoa/review", response_model=AoaReviewOut)
def review_acceptance_act(
    request: Request,
    response: Response,
    body: AoaReviewRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
    llm: Annotated[LLMClient | None, Depends(get_llm)],
) -> AoaReviewOut:
    """Ask the model about the notes the act check could not decide. 202 when a job was queued;
    200 when an earlier review of the same notes is reused, or there is nothing to ask."""
    aoa = owned_document(session, principal, body.aoa_document_id)
    contract = owned_document(session, principal, body.contract_document_id)
    queued = queue_aoa_review(request, session, settings, principal, worker, llm, aoa, contract)
    if queued.created:
        response.status_code = status.HTTP_202_ACCEPTED
    return AoaReviewOut(
        job=_job_out(queued.job) if queued.job is not None else None, items=queued.items
    )


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
    review = owned_review_job(session, principal, body.review_job_id, aoa, contract)
    report = run_aoa(
        settings,
        aoa,
        contract,
        body.contractor_type,
        body.advance(),
        review_job_id=review.id if review is not None else None,
    )

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
    return annotated_docx(settings, aoa, report)


def annotated_docx(settings: Settings, aoa: Document, report: CheckReport) -> Response:
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


# --- compare: two documents against each other ---------------------------------------------


class CompareRequest(BaseModel):
    left_document_id: str = Field(max_length=64, description="the earlier version")
    right_document_id: str = Field(max_length=64, description="the later version")
    #: A finished ``compare_explain`` job whose model answers should be merged in. The
    #: comparison itself never calls the model.
    explain_job_id: str | None = Field(default=None, max_length=64)


def owned_explain_job(
    session: Session,
    principal: Principal,
    job_id: str | None,
    left: Document,
    right: Document,
) -> Job | None:
    """The caller's explain job for exactly this pair, or None - never an error.

    The id arrives in a URL and is joined into a file path, so it is checked for shape before
    anything touches the disk, and for owner, kind and pair before its file is read: without
    this, ``../../<someone else's document>/jobs/<their job>`` read another user's stored
    answers. An id that fails any check is ignored rather than refused, exactly like an id
    whose job never finished - the deterministic report stands on its own.
    """
    if not job_id or len(job_id) > 64 or not (job_id.isascii() and job_id.isalnum()):
        return None
    job = session.get(Job, job_id)
    if (
        job is None
        or job.owner != principal.user
        or job.kind != "compare_explain"
        or job.document_id != right.id
        or job.params.get("left") != left.id
    ):
        return None
    return job


def load_explanations(settings: Settings, doc_id: str, job_id: str) -> ExplanationSet | None:
    """A stored explain run, or None when there is nothing readable there.

    Callers resolve ``job_id`` through ``owned_explain_job`` first; the shape check here is
    only a second lock on the path join.
    """
    if not (job_id.isascii() and job_id.isalnum()):
        return None
    path = result_dir(settings, doc_id) / "jobs" / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return ExplanationSet.model_validate_json(path.read_text())
    except ValueError:
        return None  # a job of another kind wrote here; not an explanation set


def _ready(doc: Document) -> StoredDocument:
    if doc.status != DocumentStatus.ready:
        msg = f"document {doc.id} is {doc.status.value}, not ready"
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    return StoredDocument(id=doc.id, kind=doc.kind, name=doc.display_name)


def run_compare(
    settings: Settings,
    left: Document,
    right: Document,
    *,
    explain_job_id: str | None = None,
) -> ComparisonReport:
    """The whole comparison. Shared by the JSON API, both downloads and the UI page.

    Deterministic and recomputed on every call — it costs milliseconds on Word documents and a
    quarter of a second on a thirteen-sheet workbook. Only the model's answers are stored, and
    they are re-validated against this fresh comparison as they are merged.
    """
    if left.id == right.id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the two documents are the same; a document compared with itself has no differences",
        )
    try:
        report = compare_stored(settings, _ready(left), _ready(right))
    except DocumentTooLargeError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    if explain_job_id:
        stored = load_explanations(settings, right.id, explain_job_id)
        if stored is not None:
            report = apply_explanations(report, stored)
    return report


@router.post("/compare")
def compare_documents(
    request: Request,
    body: CompareRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    fmt: Annotated[Literal["json", "docx", "xlsx"], Query(alias="format")] = "json",
) -> Response:
    left = owned_document(session, principal, body.left_document_id)
    right = owned_document(session, principal, body.right_document_id)
    explain = owned_explain_job(session, principal, body.explain_job_id, left, right)
    report = run_compare(settings, left, right, explain_job_id=explain.id if explain else None)

    audit(
        session,
        actor=principal.user,
        action=f"check.compare.{fmt}",
        document_id=right.id,
        ip=client_ip(request),
        detail=f"{report.counts.total_changes} change(s), {len(report.errors)} critical",
    )
    if fmt == "json":
        return Response(report.model_dump_json(), media_type="application/json")
    if fmt == "xlsx":
        return compare_xlsx(report)
    return compare_annotated_docx(settings, right, report)


def compare_xlsx(report: ComparisonReport) -> Response:
    name = f"differences-{report.left_document_id[:8]}-{report.right_document_id[:8]}.xlsx"
    return Response(
        compare_to_xlsx(report),
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Change-Count": str(report.counts.total_changes),
            "X-Error-Count": str(len(report.errors)),
        },
    )


def compare_annotated_docx(
    settings: Settings, right: Document, report: ComparisonReport
) -> Response:
    """The later version with a Word comment on each difference, saying what it said before.

    The comments go on the *right-hand* document because that is the one someone will correct.
    """
    source = upload_path(settings, right.id, right.kind)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "compared.docx"
        try:
            annotate_docx(source, report.findings, out)
        except UnsupportedDocumentError as e:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e)) from e
        data = out.read_bytes()
    return Response(
        data,
        media_type=_DOCX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="compared-{right.id[:8]}.docx"',
            "X-Change-Count": str(report.counts.total_changes),
            "X-Error-Count": str(len(report.errors)),
        },
    )


# --- BOQ: bidder workbooks against the tender template ------------------------------------


class BoqCheckRequest(BaseModel):
    template_document_id: str = Field(max_length=64)
    #: One upload per bidder. Capped because ``read_boq`` opens each workbook twice, and eleven
    #: of those on one threadpool thread is a self-inflicted stall.
    bid_document_ids: list[str] = Field(min_length=1, max_length=10)


class BoqLineRef(BaseModel):
    """One position, flattened for the wire.

    ``BidComparison`` carries ``matched: list[tuple[BoqLine, BoqLine]]`` - a large internal join
    that is never report data and never leaves the process.
    """

    model_config = ConfigDict(frozen=True)

    sheet: str
    row: int
    description: str
    item_no: str | None = None
    unit: str | None = None
    quantity: float | None = None
    template_qty: float | None = None
    bid_qty: float | None = None
    #: the bidder's own amount on the line (a filled-in blank, a priced heading)
    amount: float | None = None
    #: why the position is listed: unpriced | not_filled | non_numeric | ...
    status: str | None = None
    #: the template's wording, where the bid reworded the position
    template_description: str | None = None


class BoqBidder(BaseModel):
    bidder: str
    document_id: str
    #: The bidder's own bottom line, read off their Summary sheet. Shown, never ranked on: it
    #: carries the bidder's own quantities, added lines and whatever their spreadsheet adds.
    total_stated_incl_vat: float | None = None
    total_stated_excl_vat: float | None = None
    #: The Summary's TOTAL COST (the sections, before overheads, profit and VAT).
    total_stated_cost: float | None = None
    #: What the bidder's own position amounts add up to, sheet by sheet (excl. overheads/VAT).
    total_recomputed: float | None = None
    #: stated incl. VAT / stated TOTAL COST - the bidder's own overheads, profit and VAT
    markup_factor: float | None = None
    #: Every template position at the template quantity x the bidder's rate (GOLD_SPEC 5);
    #: added lines never count, removed and unpriced positions count nothing.
    evaluated_cost: float | None = None
    evaluated_total: float | None = None
    #: The same over the positions every bidder priced: the like-for-like number.
    common_scope_cost: float | None = None
    common_scope_total: float | None = None
    #: "red" (highest common-scope total), "green" (lowest) or None; ties share a colour
    colour: str | None = None
    #: stated incl. VAT - evaluated total, and the same by cause (each incl. the markup)
    stated_minus_evaluated: float | None = None
    difference: dict[str, float] = Field(default_factory=dict)
    #: ``common_scope_total``, or None when it cannot be computed (no stated total, no TOTAL
    #: COST, an unreadable workbook). A bidder with no basis is never ranked cheapest: None
    #: sorts last and is never coloured.
    rank_basis: float | None = None
    #: False when the bid leaves out or cannot be read for part of the template: its full
    #: evaluated total is not like-for-like (the common-scope total still is).
    comparable: bool = True
    #: False when the workbook could not be evaluated at all (saved without values)
    evaluable: bool = True
    positions_priced: int = 0
    quantity_changed: list[BoqLineRef] = Field(default_factory=list)
    filled_blank: list[BoqLineRef] = Field(default_factory=list)
    added: list[BoqLineRef] = Field(default_factory=list)
    removed: list[BoqLineRef] = Field(default_factory=list)
    added_sheets: list[str] = Field(default_factory=list)
    removed_sheets: list[str] = Field(default_factory=list)
    unpriced_sheets: list[str] = Field(default_factory=list)
    unreadable_sheets: list[str] = Field(default_factory=list)
    #: template positions with a quantity the bid leaves unpriced (status says how)
    unpriced_positions: list[BoqLineRef] = Field(default_factory=list)
    #: template positions with a blank quantity ("bidder fills in") the bid left blank
    unfilled_positions: list[BoqLineRef] = Field(default_factory=list)
    #: template positions whose unit is blank where the bid wrote one (``unit`` is the bid's);
    #: listed apart, never a unit change
    unit_filled: list[BoqLineRef] = Field(default_factory=list)
    #: rows priced in the bid where the template has no position; not evaluated
    priced_headings: list[BoqLineRef] = Field(default_factory=list)
    description_changed: list[BoqLineRef] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class BoqCheckReport(BaseModel):
    template_document_id: str
    template_name: str
    bidders: list[BoqBidder]
    #: Ascending by ``rank_basis`` (the common-scope total); unranked bidders last.
    leaderboard: list[BoqBidder]
    #: Positions in the template, and how many of them every bidder priced (the common scope).
    template_positions: int = 0
    common_scope_positions: int = 0
    #: What is wrong with or skipped in the template itself - true for every bidder alike.
    template_findings: list[Finding] = Field(default_factory=list)
    #: Top level on purpose. ``checks.boq.main`` appends these to ``comparisons[0]``, which
    #: attributes every bidder's outlier to whoever was listed first - a CLI wart, not a contract.
    rate_outliers: list[Finding] = Field(default_factory=list)
    skipped: list[SkipNote] = Field(default_factory=list)


def _ref_from_diff(d: LineDiff) -> BoqLineRef:
    item_no, _, _ = d.key.partition("|")
    return BoqLineRef(
        sheet=d.sheet,
        row=d.row,
        description=d.description,
        item_no=d.item_no or item_no or None,
        template_qty=d.template_qty,
        bid_qty=d.bid_qty,
        amount=d.amount,
        # a blank line filled in without a price that can be read: listed, never evaluated
        status=None if d.priced else "unpriced",
    )


def _ref_from_line(line: BoqLine, status: str | None = None) -> BoqLineRef:
    return BoqLineRef(
        sheet=line.sheet,
        row=line.row,
        description=line.description,
        item_no=line.item_no or None,
        unit=line.unit,
        quantity=line.quantity,
        amount=line.amount,
        status=status,
    )


def _round(value: float | None) -> float | None:
    """Money to the cent the way the BOQ check ranks and colours it (``boq.cents``): two bidders
    tied to the cent show the same cent."""
    return cents(value) if value is not None else None


def _no_basis_note(comp: BidComparison) -> SkipNote | None:
    """Why a bidder has no common-scope total - each reason said as it is."""
    if comp.common_scope_total is not None:
        return None
    if not comp.evaluable:
        ka = "ფაილი ვერ შეფასდა (იხ. დასკვნები), ამიტომ რანჟირებაში არ მონაწილეობს"
        en = "could not be evaluated (see its findings), so it is not ranked"
    elif comp.stated_incl_vat is None and comp.totals_from is None:
        ka = (
            "ფაილს Summary გვერდი არ აქვს და დღგ-ს ჩათვლით ჯამი არსად არის მითითებული, ამიტომ "
            "რანჟირებაში არ მონაწილეობს"
        )
        en = "has no Summary sheet stating a total including VAT, so it is not ranked"
    elif comp.stated_incl_vat is None and comp.totals_from == "bill":
        ka = "ცხრილის ქვეშ დღგ-ს ჩათვლით ჯამი მითითებული არ არის, ამიტომ რანჟირებაში არ მონაწილეობს"
        en = "states no total including VAT under its bill, so it is not ranked"
    elif comp.stated_incl_vat is None:
        ka = (
            "Summary გვერდზე დღგ-ს ჩათვლით ჯამი მითითებული არ არის, ამიტომ რანჟირებაში "
            "არ მონაწილეობს"
        )
        en = "states no total including VAT on its Summary sheet, so it is not ranked"
    else:
        ka = (
            "Summary გვერდზე TOTAL COST (სექციების ჯამი) არ არის, ამიტომ ზედნადები, მოგება "
            "და დღგ ვერ დადგინდა და რანჟირებაში არ მონაწილეობს"
        )
        en = (
            "states a total incl. VAT but no TOTAL COST row, so its overheads, profit and VAT "
            "cannot be applied and it is not ranked"
        )
    return SkipNote(
        rule_id="BOQ.TOTALS", reason_ka=f"{comp.bidder}: {ka}", reason_en=f"{comp.bidder}: {en}"
    )


def boq_documents(
    session: Session, principal: Principal, body: BoqCheckRequest
) -> tuple[Document, list[Document]]:
    """Resolve and validate the template plus every bid, before any workbook is opened."""
    if body.template_document_id in body.bid_document_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the template cannot also be a bid"
        )
    if len(set(body.bid_document_ids)) != len(body.bid_document_ids):
        # A repeated bidder doubles its own vote in the rate-outlier median.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "duplicate bid document ids")

    docs = [owned_document(session, principal, i) for i in body.bid_document_ids]
    template = owned_document(session, principal, body.template_document_id)
    for doc in (template, *docs):
        if doc.kind != "xlsx":
            msg = f"BOQ comparison needs .xlsx workbooks; {doc.display_name} is a {doc.kind}"
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, msg)
        if doc.status != DocumentStatus.ready:
            msg = f"document {doc.id} is {doc.status.value}, not ready"
            raise HTTPException(status.HTTP_409_CONFLICT, msg)
    return template, docs


def _read_book(settings: Settings, doc: Document) -> BoqWorkbook:
    """Always name the workbook after the upload.

    ``upload_path`` is ``{uuid}.xlsx``, so without ``name=`` the bidder is a 32-character hex id -
    which then appears inside every bilingual finding message and every cell of the merged
    workbook the client opens.
    """
    return read_boq(upload_path(settings, doc.id, doc.kind), name=Path(doc.display_name).stem[:31])


def _read_bid(settings: Settings, doc: Document) -> BoqWorkbook:
    """A bid, read so that one unreadable workbook is reported on its bidder (``BOQ.UNREADABLE``)
    instead of failing the comparison for every bidder. The template is still read strictly:
    without it there is nothing to compare against."""
    return read_bid(upload_path(settings, doc.id, doc.kind), name=Path(doc.display_name).stem[:31])


def run_boq(
    settings: Settings, template_doc: Document, bid_docs: list[Document]
) -> tuple[BoqCheckReport, BoqWorkbook, list[BoqWorkbook], list[BidComparison]]:
    """The whole comparison. Shared by the JSON API, the xlsx download and the UI page."""
    template = _read_book(settings, template_doc)
    books = [_read_bid(settings, d) for d in bid_docs]
    comparisons = compare_all(template, books)
    outliers = rate_outliers(template, comparisons)

    skipped: list[SkipNote] = []
    if len(books) < 3:
        # rate_outliers needs three quotes on a line before a median means anything. Silence
        # here would read as "no outliers found" rather than "not enough bids to look".
        skipped.append(
            SkipNote(
                rule_id="BOQ.RATE_OUTLIER",
                reason_ka=(
                    "ერთეულის ფასების შედარებას სჭირდება მინიმუმ 3 კომპანია; "
                    f"მოწოდებულია {len(books)}"
                ),
                reason_en=(f"unit-rate comparison needs at least 3 bidders; {len(books)} supplied"),
            )
        )

    bidders: list[BoqBidder] = []
    for doc, book, comp in zip(bid_docs, books, comparisons, strict=True):
        note = _no_basis_note(comp)
        if note is not None:
            skipped.append(note)
        status = {p.ref: p.status for p in comp.positions}
        bidders.append(
            BoqBidder(
                bidder=comp.bidder,
                document_id=doc.id,
                total_stated_incl_vat=comp.stated_incl_vat,
                total_stated_excl_vat=comp.stated_excl_vat,
                total_stated_cost=comp.stated_total_cost,
                total_recomputed=cents(book.total) if comp.evaluable else None,
                markup_factor=comp.markup_factor,
                evaluated_cost=cents(comp.evaluated_cost) if comp.evaluable else None,
                evaluated_total=_round(comp.evaluated_total),
                common_scope_cost=_round(comp.common_scope_cost),
                common_scope_total=_round(comp.common_scope_total),
                colour=comp.colour,
                stated_minus_evaluated=(
                    cents(comp.stated_incl_vat - comp.evaluated_total) or 0.0
                    if comp.stated_incl_vat is not None and comp.evaluated_total is not None
                    else None
                ),
                difference=comp.difference,
                rank_basis=_round(comp.common_scope_total),
                comparable=comp.complete,
                evaluable=comp.evaluable,
                positions_priced=sum(1 for p in comp.positions if p.priced),
                quantity_changed=[_ref_from_diff(d) for d in comp.quantity_changed],
                filled_blank=[_ref_from_diff(d) for d in comp.filled_blank],
                added=[_ref_from_line(line) for line in comp.added],
                removed=[_ref_from_line(line) for line in comp.removed],
                added_sheets=comp.added_sheets,
                removed_sheets=comp.removed_sheets,
                unpriced_sheets=comp.unpriced_sheets,
                unreadable_sheets=comp.unreadable_sheets,
                unpriced_positions=[
                    _ref_from_line(t, status.get((t.sheet, t.row))) for t in comp.unpriced_positions
                ],
                unfilled_positions=[
                    _ref_from_line(t, status.get((t.sheet, t.row))) for t in comp.unfilled_positions
                ],
                unit_filled=[_ref_from_line(b, "unit_filled") for b in comp.unit_filled],
                priced_headings=[_ref_from_line(b, "priced_heading") for b in comp.priced_headings],
                description_changed=[
                    _ref_from_line(b).model_copy(update={"template_description": t.description})
                    for t, b in comp.description_changed
                ],
                findings=comp.findings,
            )
        )

    report = BoqCheckReport(
        template_document_id=template_doc.id,
        template_name=template.name,
        bidders=bidders,
        leaderboard=sorted(bidders, key=lambda b: (b.rank_basis is None, b.rank_basis or 0.0)),
        template_positions=sum(len(s.leaf_lines) for s in template.sheets.values()),
        common_scope_positions=next(
            (c.common_scope_positions for c in comparisons if c.evaluable), 0
        ),
        template_findings=template_findings(template),
        rate_outliers=outliers,
        skipped=skipped,
    )
    return report, template, books, comparisons


def merged_xlsx(
    template: BoqWorkbook,
    books: list[BoqWorkbook],
    comparisons: list[BidComparison],
    template_doc: Document,
    report: BoqCheckReport,
) -> Response:
    """The merged workbook, streamed from a tempdir rather than persisted.

    A stored copy would be derived client data with no retention owner - ``sweep_expired`` only
    knows about ``Document`` rows - and regenerating it costs about two seconds.
    """
    errors = sum(
        1
        for f in (*report.template_findings, *(f for b in report.bidders for f in b.findings))
        if f.severity == "error"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "boq_merged.xlsx"
        write_merged_workbook(template, books, comparisons, out, outliers=report.rate_outliers)
        data = out.read_bytes()
    return Response(
        data,
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="boq-merged-{template_doc.id[:8]}.xlsx"',
            "X-Bidder-Count": str(len(books)),
            "X-Error-Count": str(errors),
        },
    )


@router.post("/boq")
def check_boq(
    request: Request,
    body: BoqCheckRequest,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    fmt: Annotated[Literal["json", "xlsx"], Query(alias="format")] = "json",
) -> Response:
    """Compare bidder workbooks against the tender template.

    Synchronous, and deliberately a plain ``def``: ``read_boq`` is two ``load_workbook`` passes
    per file, so this is seconds of blocking CPU that belongs on the threadpool, not on the
    event loop. It creates no job - a BOQ run spans N+1 documents, and ``Job.document_id`` is a
    single FK, so a job would have to pick one arbitrarily.
    """
    template_doc, bid_docs = boq_documents(session, principal, body)
    report, template, books, comparisons = run_boq(settings, template_doc, bid_docs)
    errors = sum(
        1
        for f in (*report.template_findings, *(f for b in report.bidders for f in b.findings))
        if f.severity == "error"
    )

    audit(
        session,
        actor=principal.user,
        action=f"check.boq.{fmt}",
        document_id=template_doc.id,
        ip=client_ip(request),
        detail=f"{len(books)} bidder(s), {errors} error(s)",
    )
    if fmt == "json":
        return Response(report.model_dump_json(), media_type="application/json")
    return merged_xlsx(template, books, comparisons, template_doc, report)

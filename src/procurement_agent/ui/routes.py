"""Minimal server-rendered UI. No JavaScript frameworks, no CDN — everything is served locally."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, cast, get_args
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy.orm import Session

from procurement_agent.api import documents as docs_api
from procurement_agent.api.checks import (
    BoqCheckRequest,
    annotated_docx,
    boq_documents,
    compare_annotated_docx,
    compare_xlsx,
    merged_xlsx,
    owned_explain_job,
    owned_review_job,
    queue_aoa_review,
    run_aoa,
    run_boq,
    run_compare,
)
from procurement_agent.api.deps import (
    client_ip,
    ensure_job_capacity,
    get_llm,
    get_worker,
    owned_document,
    owned_documents,
    owned_job,
)
from procurement_agent.api.extraction import ExtractRequest, start_extraction
from procurement_agent.checks.aoa import ContractorType
from procurement_agent.checks.compare import ComparisonReport, Hunk, ValueDelta
from procurement_agent.checks.findings import Finding
from procurement_agent.checks.fx_invoice import parse_advance
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.pipeline import result_dir
from procurement_agent.extraction.schemas import DEFAULT_TARGETS, ExtractionResult, FactType
from procurement_agent.jobs.worker import Worker
from procurement_agent.models import Document, Job, JobStatus
from procurement_agent.security.audit import audit, audit_now
from procurement_agent.security.auth import (
    COOKIE_NAME,
    Principal,
    authenticate,
    create_ui_session,
    is_dev_key,
    is_same_origin,
    lookup_api_key,
    lookup_ui_session,
    revoke_ui_session,
)
from procurement_agent.ui.i18n import LANG_COOKIE, Lang, lang_of, t

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)
_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(default=True, default_for_string=True),
    undefined=StrictUndefined,
)
templates = Jinja2Templates(env=_env)
_STATIC = Path(__file__).parent / "static"


def _asset_version() -> str:
    """A content hash of the static files, appended to every asset URL.

    The pages are no-store but /static is not, so without it an upgrade would pair new markup
    with last week's stylesheet until someone thought to hard-reload.
    """
    digest = hashlib.sha256()
    for path in sorted(_STATIC.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


_ASSET_V = _asset_version()


def _icon(name: str) -> Markup:
    """An icon from the sprite. Decorative by construction: every icon sits beside a word."""
    return Markup(
        '<svg class="i" aria-hidden="true" focusable="false">'
        '<use href="/static/icons.svg?v={}#{}"></use></svg>'
    ).format(_ASSET_V, name)


def _money(value: float | None) -> str:
    """Thousands separators, the format the check messages already use (``2,800.00``).

    On screen only: the merged workbook keeps real numbers so Excel can still sum them.
    """
    return "—" if value is None else f"{value:,.2f}"


_env.globals["icon"] = _icon
_env.globals["asset_v"] = _ASSET_V
_env.filters["money"] = _money


def _render(request: Request, name: str, **ctx: object) -> Response:
    """Every UI response goes through here, which is why the language lives here.

    Injecting ``lang``/``t``/``msg``/``current_path`` centrally makes each template
    StrictUndefined-safe by construction - a new page cannot forget them and 500 on render.
    """
    lang = lang_of(request.cookies.get(LANG_COOKIE))
    ctx.setdefault("lang", lang)
    ctx.setdefault("t", lambda key, **kw: t(key, lang).format(**kw) if kw else t(key, lang))
    ctx.setdefault("msg", lambda f: f.message_ka if lang == "ka" else f.message_en)
    ctx.setdefault("why", lambda s: s.reason_ka if lang == "ka" else s.reason_en)
    ctx.setdefault("current_path", _current_path(request))
    resp: Response = templates.TemplateResponse(request, name, ctx)
    return resp


def _current_path(request: Request) -> str:
    """Path plus query, so the language toggle returns you to the report you were reading."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


@router.get("/")
def root(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """The task picker is the front door: the question is what you want to do, not what files
    you happen to have uploaded. The last few uploads sit underneath it, so coming back to
    yesterday's work is one click rather than a trip through the documents list."""
    recent = owned_documents(session, principal, limit=5)
    return _render(request, "picker.html", user=principal.user, recent=recent)


@router.post("/lang")
def set_language(
    request: Request,
    lang: Annotated[str, Form(max_length=8)],
    next_: Annotated[str | None, Form(alias="next", max_length=500)] = None,
) -> Response:
    """A form post, so the toggle works without JavaScript.

    No sign-in required: the toggle is on the sign-in page too, where requiring a session
    answered an English reader's first click with a JSON 401. It sets a display preference and
    redirects inside the UI only, so the same-origin check is the whole guard it needs.
    """
    if not is_same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused")
    resp = RedirectResponse(_safe_next(next_), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        LANG_COOKIE,
        lang_of(lang),
        max_age=365 * 24 * 3600,
        httponly=False,  # a display preference, not a credential
        samesite="strict",
        path="/",
    )
    return resp


# --- login -------------------------------------------------------------------------------------


def _safe_next(target: str | None) -> str:
    """Only ever redirect back to a path inside this UI.

    Anything else (absolute URL, protocol-relative ``//evil.com``, backslash trickery) falls
    back to the document list, so ``?next=`` can never become an open redirect.
    """
    if not target or not target.startswith("/ui/"):
        return "/ui/"
    if target.startswith("//") or "\\" in target or target.startswith("/ui/login"):
        return "/ui/"
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
    """Check the key once and hand the browser a server-side session, never the key itself.

    Same-origin only: a cross-site form could otherwise sign a victim into the attacker's
    account (login CSRF) and collect whatever they then upload.
    """
    ip = client_ip(request)
    if not is_same_origin(request):
        audit_now(
            actor="anonymous", action="auth.csrf", outcome="denied", ip=ip, detail="POST /ui/login"
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused")
    dev_ok = is_dev_key(settings, api_key)
    row = None if dev_ok else lookup_api_key(session, api_key)
    if not dev_ok and row is None:
        audit(session, actor="anonymous", action="ui.login", outcome="denied", ip=ip)
        return _render(
            request, "login.html", error=t("login.invalid", _lang(request)), next=_safe_next(next_)
        )
    # A cookie from an earlier sign-in in this browser is retired, not left valid beside the
    # new one.
    old = request.cookies.get(COOKIE_NAME)
    if old and (previous := lookup_ui_session(session, settings, old)) is not None:
        revoke_ui_session(session, previous.id)
    user = "dev" if row is None else row.user
    token = create_ui_session(
        session, settings, user=user, api_key_id=None if row is None else row.id
    )
    resp = RedirectResponse(_safe_next(next_), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.env == "prod",
        max_age=settings.ui_session_hours * 3600,
        path="/",
    )
    audit(session, actor=user, action="ui.login", ip=ip)
    # Committed before the redirect goes out: the browser follows it at once, and a session
    # row still waiting on the dependency's exit would answer that first request with a 401.
    session.commit()
    return resp


@router.post("/logout")
def logout(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Revoke the session on the server; deleting the cookie alone left a copy of it valid."""
    if principal.session_id is not None:
        revoke_ui_session(session, principal.session_id)
    audit(session, actor=principal.user, action="ui.logout", ip=client_ip(request))
    session.commit()  # revoked before the browser is told it has signed out
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
    docs = owned_documents(session, principal, limit=100)
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
        return _error(request, principal, e.detail)
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
    settings: Annotated[Settings, Depends(get_settings)],
    targets: Annotated[list[str] | None, Form()] = None,
    question: Annotated[str | None, Form(max_length=500)] = None,
    fast: Annotated[bool, Form()] = False,
    # False, not True: an unticked HTML checkbox sends nothing at all, so a True default made
    # "don't use the model" impossible to choose. The box is unticked by default: the model is
    # opt-in (api/extraction.py says why).
    use_llm: Annotated[bool, Form()] = False,
) -> Response:
    valid = [FactType(t) for t in (targets or []) if t in FactType.__members__]
    try:
        body = ExtractRequest(
            targets=valid or None,
            question=(question or "").strip() or None,
            fast=fast,
            use_llm=use_llm,
        )
        job = start_extraction(request, doc_id, body, principal, session, worker, settings)
    except ValidationError:
        return _error(request, principal, t("error.question", _lang(request)))
    except HTTPException as e:
        if e.status_code == status.HTTP_404_NOT_FOUND:
            return _expired(request, principal, "/ui/documents")
        return _error(request, principal, e.detail)
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
    try:
        job = owned_job(session, principal, job_id, live_document=True)
    except HTTPException:
        return _expired(request, principal, "/ui/documents")
    result: ExtractionResult | None = None
    if job.kind == "extract" and job.status == JobStatus.done:
        path = result_dir(settings, job.document_id) / "jobs" / f"{job.id}.json"
        if path.exists():
            result = ExtractionResult.model_validate_json(path.read_text())
    return _render(
        request,
        "job.html",
        user=principal.user,
        job=job,
        doc=job.document,
        result=result,
        # the stored label is English; the Georgian page names each field in Georgian
        labels=LABELS,
    )


# --- tasks: upload steps -----------------------------------------------------------------------


@router.get("/start/aoa")
def start_aoa(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    return _render(
        request, "start_aoa.html", user=principal.user, contractor_types=get_args(ContractorType)
    )


@router.get("/start/boq")
def start_boq(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    return _render(request, "start_boq.html", user=principal.user)


@router.post("/start/aoa")
async def start_aoa_without_js(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
    act: UploadFile,
    contract: UploadFile | None = None,
    contractor_type: Annotated[str | None, Form()] = None,
    advance_amount: Annotated[str | None, Form()] = None,
    advance_date: Annotated[str | None, Form()] = None,
) -> Response:
    """The same flow without JavaScript: two uploads server-side, then the result URL.

    Only the act check degrades this cleanly - BOQ needs N files and the upload endpoint takes
    one, so its template carries a <noscript> pointing at the documents page instead.
    """
    try:
        act_out = await docs_api.upload_document(request, act, principal, session, settings, worker)
        contract_id = None
        if contract is not None and contract.filename:
            out = await docs_api.upload_document(
                request, contract, principal, session, settings, worker
            )
            contract_id = out.document.id
    except HTTPException as e:
        return _error(request, principal, e.detail)

    params = {"act": act_out.document.id}
    if contract_id:
        params["contract"] = contract_id
    if contractor_type:
        params["contractor_type"] = contractor_type
    # passed on as typed: the report reads them, and leaves out what does not read
    for name, value in (("advance_amount", advance_amount), ("advance_date", advance_date)):
        if value and value.strip():
            params[name] = value.strip()
    return RedirectResponse(
        f"/ui/checks/aoa?{urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER
    )


# --- tasks: reports ----------------------------------------------------------------------------


def _ids(raw: str | None, limit: int) -> list[str]:
    """Document ids out of a query string. Anything malformed would only ever 404, so drop it."""
    out: list[str] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if token and len(token) <= 64 and token.isalnum() and token not in out:
            out.append(token)
    return out[:limit]


@router.get("/checks/aoa")
def aoa_report(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    act: str,
    contract: str | None = None,
    contractor_type: str | None = None,
    advance_amount: str | None = None,
    advance_date: str | None = None,
    review: str | None = None,
) -> Response:
    """The rules' report, recomputed on every view. A finished model review rides in
    ``review`` and is applied to the notes the rules could not decide; its absence means
    nobody asked the model."""
    try:
        aoa_doc, contract_doc, ctype = _aoa_inputs(
            session, principal, act, contract, contractor_type
        )
        advance = parse_advance(advance_amount, advance_date)
        review_row = owned_review_job(session, principal, review, aoa_doc, contract_doc)
        report = run_aoa(
            settings,
            aoa_doc,
            contract_doc,
            ctype,
            advance,
            review_job_id=review_row.id if review_row is not None else None,
        )
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/aoa")
    audit(
        session,
        actor=principal.user,
        action="check.aoa.json",
        document_id=aoa_doc.id,
        ip=client_ip(request),
        detail=f"{len(report.errors)} error(s), {len(report.findings)} finding(s)",
    )
    review_status = review_row.status.value if review_row is not None else None
    resp = _render(
        request,
        "check_aoa.html",
        user=principal.user,
        report=report,
        act=aoa_doc,
        contract=contract_doc,
        contractor_type=ctype,
        contractor_types=get_args(ContractorType),
        query=request.url.query,
        groups=_by_severity(report.findings),
        # the model review: offered only where it can run, and only for notes left open
        can_review=settings.aoa_llm_enabled
        and get_llm(request) is not None
        and contract_doc is not None,
        review_items=int(report.summary.get("review_items", "0")),
        review_job=review_row.id if review_row is not None else None,
        review_status=review_status,
        review_running=review_status in (JobStatus.queued.value, JobStatus.running.value),
        review_state=report.summary.get("review"),
        review_marked=any(f.reviewed_by for f in report.findings),
        review_auto=settings.aoa_llm_auto,
    )
    return resp  # no-store: set for every /ui/ response in main._security_headers


@router.get("/checks/aoa/annotated.docx")
def aoa_annotated(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    act: str,
    contract: str | None = None,
    contractor_type: str | None = None,
    advance_amount: str | None = None,
    advance_date: str | None = None,
    review: str | None = None,
) -> Response:
    """A plain link, not a JS blob: works with JavaScript off and needs no CSP argument. The
    report's own query rides along, so the Word file carries the model's reading when the page
    does."""
    try:
        aoa_doc, contract_doc, ctype = _aoa_inputs(
            session, principal, act, contract, contractor_type
        )
        advance = parse_advance(advance_amount, advance_date)
        review_row = owned_review_job(session, principal, review, aoa_doc, contract_doc)
        report = run_aoa(
            settings,
            aoa_doc,
            contract_doc,
            ctype,
            advance,
            review_job_id=review_row.id if review_row is not None else None,
        )
        # 415 for a scan or a spreadsheet: an HTML navigation must not land on raw JSON.
        resp = annotated_docx(settings, aoa_doc, report)
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/aoa")
    audit(
        session,
        actor=principal.user,
        action="check.aoa.docx",
        document_id=aoa_doc.id,
        ip=client_ip(request),
        detail=f"{len(report.errors)} error(s), {len(report.findings)} finding(s)",
    )
    return resp


@router.post("/checks/aoa/review")
def aoa_review(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
    act: str,
    contract: str | None = None,
    contractor_type: str | None = None,
    advance_amount: str | None = None,
    advance_date: str | None = None,
) -> Response:
    """Queue the model review of the notes the rules could not decide, then go back to the
    report. Nothing else on the act's report ever calls the model.

    The redirect rebuilds the report's own URL - the contractor type and the advance included -
    and adds the review's id, so the page the user comes back to is the one they left, with the
    model's reading on it once the job is done. A review of the same notes already asked is
    reused (``queue_aoa_review``), so pressing again costs nothing.
    """
    llm = get_llm(request)
    if not settings.aoa_llm_enabled:
        return _error(request, principal, t("error.review_disabled", _lang(request)))
    if llm is None:
        return _error(request, principal, t("error.review_unavailable", _lang(request)))
    try:
        aoa_doc, contract_doc, ctype = _aoa_inputs(
            session, principal, act, contract, contractor_type
        )
        queued = (
            queue_aoa_review(
                request, session, settings, principal, worker, llm, aoa_doc, contract_doc
            )
            if contract_doc is not None
            else None
        )
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/aoa")

    params = {"act": aoa_doc.id}
    if contract_doc is not None:
        params["contract"] = contract_doc.id
    if ctype:
        params["contractor_type"] = ctype
    for name, value in (("advance_amount", advance_amount), ("advance_date", advance_date)):
        if value and value.strip():
            params[name] = value.strip()
    if queued is not None and queued.job is not None:
        params["review"] = queued.job.id
    return RedirectResponse(
        f"/ui/checks/aoa?{urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/checks/boq")
def boq_report(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    template: str,
    bids: str,
) -> Response:
    """Plain ``def``: reading N workbooks is seconds of blocking CPU that belongs on the
    threadpool, not on the event loop."""
    try:
        template_doc, bid_docs = _boq_inputs(session, principal, template, bids)
    except ValidationError:
        return _error(request, principal, t("error.boq_inputs", _lang(request)))
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/boq")

    report, _, _, _ = run_boq(settings, template_doc, bid_docs)
    audit(
        session,
        actor=principal.user,
        action="check.boq.json",
        document_id=template_doc.id,
        ip=client_ip(request),
        detail=f"{len(bid_docs)} bidder(s)",
    )
    # Rank every bidder who stated a total. Excluding the not-comparable ones as well was too
    # strict for real tenders: on the client's own set all four left a section unpriced, which
    # suppressed the highest-red / lowest-green rule they explicitly asked for. The caveat
    # belongs on the row as a badge, not in the silent removal of the ranking.
    ranked = [b for b in report.leaderboard if b.rank_basis is not None]
    resp = _render(
        request,
        "check_boq.html",
        user=principal.user,
        report=report,
        query=request.url.query,
        lowest=ranked[0].bidder if len(ranked) > 1 else None,
        highest=ranked[-1].bidder if len(ranked) > 1 else None,
        groups_for=lambda bidder: _by_severity(bidder.findings),
        caveats=[b for b in report.bidders if not b.comparable],
    )
    return resp  # no-store: set for every /ui/ response in main._security_headers


@router.get("/checks/boq/merged.xlsx")
def boq_merged(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    template: str,
    bids: str,
) -> Response:
    try:
        template_doc, bid_docs = _boq_inputs(session, principal, template, bids)
    except ValidationError:
        return _error(request, principal, t("error.boq_inputs", _lang(request)))
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/boq")

    report, book, books, comparisons = run_boq(settings, template_doc, bid_docs)
    audit(
        session,
        actor=principal.user,
        action="check.boq.xlsx",
        document_id=template_doc.id,
        ip=client_ip(request),
        detail=f"{len(bid_docs)} bidder(s)",
    )
    return merged_xlsx(book, books, comparisons, template_doc, report)


# --- tasks: comparing two documents ------------------------------------------------------------

#: Unchanged lines kept either side of a change before the rest of the run is folded away. Three
#: is what ``diff -u`` shows, and the reason is the same: one line of context is not enough to
#: recognise where you are in a contract.
_CONTEXT = 3


@router.get("/start/compare")
def start_compare(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    return _render(request, "start_compare.html", user=principal.user)


@router.post("/start/compare")
async def start_compare_without_js(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
    left: UploadFile,
    right: UploadFile,
) -> Response:
    """The same flow without JavaScript. Two files, like the act check, so this degrades."""
    try:
        a = await docs_api.upload_document(request, left, principal, session, settings, worker)
        b = await docs_api.upload_document(request, right, principal, session, settings, worker)
    except HTTPException as e:
        return _error(request, principal, e.detail)
    params = {"left": a.document.id, "right": b.document.id}
    return RedirectResponse(
        f"/ui/checks/compare?{urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER
    )


def _fold(rows: list[object], show: str) -> list[dict[str, object]]:
    """Group diff rows into blocks the template renders, folding long unchanged runs.

    Under a filter the unchanged runs are dropped outright rather than folded: a reader who
    asked for "only critical" does not want forty collapsed accordions between the two rows
    they asked for.
    """
    if show != "all":
        keep = [r for r in rows if getattr(r, "hunk_id", None)]
        return [{"fold": False, "rows": keep}] if keep else []

    blocks: list[dict[str, object]] = []
    run: list[object] = []

    def flush() -> None:
        if not run:
            return
        if len(run) > 2 * _CONTEXT + 1:
            blocks.append({"fold": False, "rows": run[:_CONTEXT]})
            blocks.append({"fold": True, "rows": run[_CONTEXT:-_CONTEXT]})
            blocks.append({"fold": False, "rows": run[-_CONTEXT:]})
        else:
            blocks.append({"fold": False, "rows": list(run)})
        run.clear()

    for row in rows:
        if getattr(row, "tag", "") == "equal":
            run.append(row)
            continue
        flush()
        if blocks and not blocks[-1]["fold"]:
            cast("list[object]", blocks[-1]["rows"]).append(row)
        else:
            blocks.append({"fold": False, "rows": [row]})
    flush()
    return blocks


def _matters(report: ComparisonReport, delta_msg: object) -> list[dict[str, object]]:
    """The values a buyer reads first: every critical difference, located where possible.

    Hunk deltas come first because they can point at a line; the document-wide field deltas
    follow and are deduplicated against them. That ordering is what makes the panel useful on
    an unrelated pair, where there are no meaningful lines to point at at all.
    """
    out: list[dict[str, object]] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    def add(delta: ValueDelta, hunk: Hunk | None) -> None:
        key = (delta.rule_id, delta.left, delta.right)
        if key in seen:
            return
        seen.add(key)
        where = None
        if hunk is not None:
            ref = hunk.right or hunk.left
            where = ref.path if ref is not None else None
        out.append(
            {
                "label": delta.label_ka,
                "left": delta.left,
                "right": delta.right,
                "change": _change_text(delta),
                "severity": delta.severity,
                "where": where,
                "anchor": hunk.id if hunk is not None else "",
            }
        )

    for hunk in report.hunks:
        if hunk.materiality != "critical":
            continue
        for delta in hunk.deltas:
            add(delta, hunk)
    for delta in report.field_deltas:
        add(delta, None)
    return out


def _change_text(delta: ValueDelta) -> str:
    """The one column that says *how much*, rather than repeating both values."""
    bits: list[str] = []
    if delta.delta is not None:
        sign = "+" if delta.delta > 0 else ""
        bits.append(f"{sign}{delta.delta:g}")
        if delta.delta_pct is not None:
            bits.append(f"{sign}{delta.delta_pct:.1f}%")
    if delta.days is not None:
        bits.append(f"{delta.days:+d} d")
    return " · ".join(bits)


def _compare_inputs(
    session: Session, principal: Principal, left: str, right: str
) -> tuple[Document, Document]:
    return (
        owned_document(session, principal, left),
        owned_document(session, principal, right),
    )


@router.get("/checks/compare")
def compare_report(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    left: str,
    right: str,
    show: str = "all",
    job: str | None = None,
) -> Response:
    """Plain ``def``: segmenting two documents is blocking CPU that belongs on the threadpool.

    The comparison is recomputed here on every request - milliseconds on Word, a quarter of a
    second on a thirteen-sheet workbook - so the URL stays the whole state. A finished explain
    job rides in ``job`` and is merged in; its absence simply means nobody asked the model yet.
    """
    if show not in ("all", "changes", "critical"):
        show = "all"
    try:
        left_doc, right_doc = _compare_inputs(session, principal, left, right)
        explain_row = owned_explain_job(session, principal, job, left_doc, right_doc)
        job = explain_row.id if explain_row is not None else None
        report = run_compare(settings, left_doc, right_doc, explain_job_id=job)
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/compare")

    lang = lang_of(request.cookies.get(LANG_COOKIE))
    running = explain_row is not None and explain_row.status in (
        JobStatus.queued,
        JobStatus.running,
    )

    def delta_msg(delta: ValueDelta) -> str:
        label = delta.label_ka if lang == "ka" else delta.label_en
        note = delta.note_ka if lang == "ka" else delta.note_en
        body = (
            f"{delta.left} → {delta.right}"
            if delta.kind == "changed"
            else (delta.right or delta.left or "")
        )
        return f"{label}: {body}" + (f" ({note})" if note else "")

    audit(
        session,
        actor=principal.user,
        action="check.compare.json",
        document_id=right_doc.id,
        ip=client_ip(request),
        detail=f"{report.counts.total_changes} change(s), {len(report.errors)} critical",
    )
    query = urlencode({"left": left_doc.id, "right": right_doc.id, **({"job": job} if job else {})})
    resp = _render(
        request,
        "check_compare.html",
        user=principal.user,
        report=report,
        left=left_doc,
        right=right_doc,
        query=query,
        show=show,
        blocks=_fold(list(report.rows), show),
        matters=_matters(report, delta_msg),
        groups=_by_severity(report.findings),
        can_explain=settings.compare_llm_enabled,
        unexplained=len(report.unexplained),
        explain_job=job,
        explain_running=running,
        d={
            "t": lambda key, **kw: t(key, lang).format(**kw) if kw else t(key, lang),
            "hunks": {h.id: h for h in report.hunks},
            "delta_msg": delta_msg,
            "explanation": lambda h: h.explanation_ka if lang == "ka" else h.explanation_en,
            "sev_of": _severity_of,
            "can_explain": settings.compare_llm_enabled,
        },
    )
    return resp  # no-store: set for every /ui/ response in main._security_headers


def _severity_of(hunk: Hunk) -> str:
    """The finding vocabulary, reused so a hunk and its finding cannot be coloured differently."""
    return {"critical": "error", "substantive": "warning"}.get(hunk.materiality, "info")


@router.post("/checks/compare/explain")
def compare_explain(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    worker: Annotated[Worker, Depends(get_worker)],
    left: str,
    right: str,
) -> Response:
    """Queue the model pass. Nothing else on this screen ever calls the model.

    The job is keyed on the right-hand document because ``Job.document_id`` is a single foreign
    key; the left id travels in ``params``. Its id then rides in the report URL, so a bookmark
    made after the run still shows what the model said.
    """
    if not settings.compare_llm_enabled:
        return _error(request, principal, t("error.llm_disabled", _lang(request)))
    try:
        left_doc, right_doc = _compare_inputs(session, principal, left, right)
        ensure_job_capacity(session, settings, principal)
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/compare")

    row = Job(
        document_id=right_doc.id,
        owner=principal.user,
        kind="compare_explain",
        params={"left": left_doc.id, "right": right_doc.id},
    )
    session.add(row)
    session.flush()
    audit(
        session,
        actor=principal.user,
        action="check.compare.explain",
        document_id=right_doc.id,
        job_id=row.id,
        ip=client_ip(request),
    )
    job_id = row.id
    session.commit()
    worker.submit(job_id)
    params = urlencode({"left": left_doc.id, "right": right_doc.id, "job": job_id})
    return RedirectResponse(f"/ui/checks/compare?{params}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/checks/compare/annotated.docx")
def compare_annotated(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    left: str,
    right: str,
    job: str | None = None,
) -> Response:
    """The revised document with a Word comment on each difference. A plain link, not a blob."""
    try:
        left_doc, right_doc = _compare_inputs(session, principal, left, right)
        explain = owned_explain_job(session, principal, job, left_doc, right_doc)
        report = run_compare(
            settings, left_doc, right_doc, explain_job_id=explain.id if explain else None
        )
        resp = compare_annotated_docx(settings, right_doc, report)
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/compare")
    audit(
        session,
        actor=principal.user,
        action="check.compare.docx",
        document_id=right_doc.id,
        ip=client_ip(request),
        detail=f"{len(report.findings)} finding(s)",
    )
    return resp


@router.get("/checks/compare/differences.xlsx")
def compare_differences(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    left: str,
    right: str,
    job: str | None = None,
) -> Response:
    try:
        left_doc, right_doc = _compare_inputs(session, principal, left, right)
        explain = owned_explain_job(session, principal, job, left_doc, right_doc)
        report = run_compare(
            settings, left_doc, right_doc, explain_job_id=explain.id if explain else None
        )
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/compare")
    audit(
        session,
        actor=principal.user,
        action="check.compare.xlsx",
        document_id=right_doc.id,
        ip=client_ip(request),
        detail=f"{report.counts.total_changes} change(s)",
    )
    return compare_xlsx(report)


# --- shared helpers ----------------------------------------------------------------------------


def _aoa_inputs(
    session: Session, principal: Principal, act: str, contract: str | None, ctype: str | None
) -> tuple[Document, Document | None, ContractorType | None]:
    aoa_doc = owned_document(session, principal, act)
    contract_doc = owned_document(session, principal, contract) if contract else None
    valid = ctype if ctype in get_args(ContractorType) else None
    return aoa_doc, contract_doc, cast("ContractorType | None", valid)


def _boq_inputs(
    session: Session, principal: Principal, template: str, bids: str
) -> tuple[Document, list[Document]]:
    """Raises ``ValidationError`` for an empty bid list or an over-long template id - both
    reachable by editing the URL - which the callers turn into a page, not a 500."""
    body = BoqCheckRequest(template_document_id=template, bid_document_ids=_ids(bids, 10))
    return boq_documents(session, principal, body)


def _expired(request: Request, principal: Principal, again: str) -> Response:
    """A retention sweep - or a link shared with a colleague - and the documents are gone.

    ``owned_document`` returns the same 404 for a deleted document and for one belonging to
    someone else, so this page says "no longer available to you" rather than claiming deletion.
    Without it the browser would render the raw JSON 404: the handler in ``main`` only
    special-cases 401.
    """
    return _render(request, "expired.html", user=principal.user, again=again)


def _error(request: Request, principal: Principal, message: object) -> Response:
    return _render(request, "error.html", user=principal.user, message=message)


def _http_error(request: Request, principal: Principal, exc: HTTPException, again: str) -> Response:
    """Map an API refusal onto a page: 404 means the documents are gone (or never were the
    caller's), anything else - not ready yet, wrong file type, too many jobs - says why."""
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return _expired(request, principal, again)
    return _error(request, principal, exc.detail)


def _lang(request: Request) -> Lang:
    return lang_of(request.cookies.get(LANG_COOKIE))


def _by_severity(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    order = ("error", "warning", "info")
    groups = [(s, [f for f in findings if f.severity == s]) for s in order]
    return [(s, items) for s, items in groups if items]

"""Minimal server-rendered UI. No JavaScript frameworks, no CDN — everything is served locally."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, cast, get_args
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from procurement_agent import history
from procurement_agent.api import documents as docs_api
from procurement_agent.api.checks import (
    BoqCheckRequest,
    annotated_docx,
    annotated_pdf,
    boq_documents,
    compare_annotated_docx,
    compare_xlsx,
    explain_answered,
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
from procurement_agent.api.refusals import audit_or_503
from procurement_agent.checks.aoa import ContractorType
from procurement_agent.checks.compare import ComparisonReport, Hunk, ValueDelta
from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.checks.fx_invoice import (
    format_entered_rates,
    parse_advance,
    parse_entered_rates,
)
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.extraction.labels import LABELS
from procurement_agent.extraction.pipeline import is_scanned_pdf, load_segments, result_dir
from procurement_agent.extraction.read_status import (
    LLM_UNAVAILABLE,
    StoredExtraction,
    partial_notes,
)
from procurement_agent.extraction.schemas import DEFAULT_TARGETS, FactType
from procurement_agent.ingestion import read_quality
from procurement_agent.jobs.stuck import fail_unreadable
from procurement_agent.jobs.worker import Worker
from procurement_agent.models import CheckRun, Document, DocumentStatus, Job, JobStatus
from procurement_agent.security.audit import audit, audit_now, log
from procurement_agent.security.auth import (
    Principal,
    authenticate,
    cookie_name,
    create_ui_session,
    is_dev_key,
    is_same_origin,
    lookup_api_key,
    lookup_ui_session,
    revoke_ui_session,
)
from procurement_agent.ui import reading
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


def _document_error(error: str | None, lang: Lang) -> str:
    """A failed document's ``error`` for the page. Our own codes (``NO_TEXT`` and the hint that
    names the cause) are said in the page's language, the code kept in front so it can be quoted;
    the free text of any other failure is shown as stored."""
    code, hint = read_quality.split_error(error)
    if code is None:
        return error or ""
    text = t(f"error.{code}", lang)
    if hint:
        text = f"{text} {t(f'error.hint.{hint}', lang)}"
    return f"{code}: {text}"


_env.filters["doc_error"] = _document_error


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
    settings: Annotated[Settings, Depends(get_settings)],
    next_: Annotated[str | None, Form(alias="next", max_length=500)] = None,
) -> Response:
    """A form post, so the toggle works without JavaScript.

    No sign-in required: the toggle is on the sign-in page too, where requiring a session
    answered an English reader's first click with a JSON 401. It sets a display preference and
    redirects inside the UI only, so the same-origin check is the whole guard it needs.
    """
    if not is_same_origin(request):
        audit_now(
            actor="anonymous",
            action="auth.csrf",
            outcome="denied",
            ip=client_ip(request),
            detail="POST /ui/lang",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused")
    resp = RedirectResponse(_safe_next(next_), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        LANG_COOKIE,
        lang_of(lang),
        max_age=365 * 24 * 3600,
        httponly=False,  # a display preference, not a credential
        samesite="strict",
        secure=settings.env == "prod",
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
    old = request.cookies.get(cookie_name(settings))
    if old and (previous := lookup_ui_session(session, settings, old)) is not None:
        revoke_ui_session(session, previous.id)
    user = "dev" if row is None else row.user
    token = create_ui_session(
        session, settings, user=user, api_key_id=None if row is None else row.id
    )
    resp = RedirectResponse(_safe_next(next_), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        cookie_name(settings),
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Revoke the session on the server; deleting the cookie alone left a copy of it valid."""
    if principal.session_id is not None:
        revoke_ui_session(session, principal.session_id)
    audit(session, actor=principal.user, action="ui.logout", ip=client_ip(request))
    session.commit()  # revoked before the browser is told it has signed out
    resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=settings.env == "prod",
        httponly=True,
        samesite="strict",
    )
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    doc = owned_document(session, principal, doc_id)
    stored = (
        read_quality.read_sidecar(settings, doc.id) if doc.status == DocumentStatus.ready else None
    )
    jobs = sorted(doc.jobs, key=lambda j: j.created_at, reverse=True)
    targets = [(t.value, LABELS[t][0], LABELS[t][1]) for t in DEFAULT_TARGETS]
    return _render(
        request,
        "document.html",
        user=principal.user,
        doc=doc,
        jobs=jobs,
        targets=targets,
        checks=_checks_for(session, principal, doc),
        read_notes=partial_notes({"document": stored}),
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


#: Jobs whose answer is read in a report: the model's explanation of a comparison, or its review
#: of an act's notes, is applied there, so that is where their row leads.
_REPORT_JOBS = ("compare_explain", "aoa_review")


@router.get("/jobs/{job_id}")
def job_page(
    request: Request,
    job_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    part: str = "1",
) -> Response:
    """The result of one job, whatever it was.

    Reading shows what was read from the file, extraction its fields, and the two model jobs
    send you on to the report they belong to. ``part`` pages through a long reading; it is read
    by hand, so a link edited into nonsense still opens the page.
    """
    try:
        job = owned_job(session, principal, job_id, live_document=True)
    except HTTPException:
        return _expired(request, principal, "/ui/documents")
    if job.kind in _REPORT_JOBS:
        return RedirectResponse(
            _report_job_url(session, job), status_code=status.HTTP_303_SEE_OTHER
        )
    if job.kind == "parse":
        return _reading_page(request, principal, session, settings, job, reading.parse_part(part))
    result: StoredExtraction | None = None
    gone = False
    if job.kind == "extract" and job.status == JobStatus.done:
        path = result_dir(settings, job.document_id) / "jobs" / f"{job.id}.json"
        try:
            result = StoredExtraction.model_validate_json(path.read_text())
        except (OSError, ValueError):
            gone = True  # swept or damaged: say so, rather than a header and nothing under it
        else:
            # Showing the extracted values is reading the document's content, like the API's
            # ``result.read.*`` and the reading page's ``document.content_view``.
            if (
                refused := _audited(
                    request,
                    principal,
                    session,
                    action="result.read.ui",
                    document_id=job.document_id,
                    job_id=job.id,
                )
            ) is not None:
                return refused
    return _render(
        request,
        "job.html",
        user=principal.user,
        job=job,
        doc=job.document,
        result=result,
        read_notes=partial_notes(result.read_quality) if result is not None else [],
        gone=gone,
        # the stored label is English; the Georgian page names each field in Georgian
        labels=LABELS,
    )


def _reading_page(
    request: Request,
    principal: Principal,
    session: Session,
    settings: Settings,
    job: Job,
    part: int,
) -> Response:
    """What the reading job produced: the file's content as the system took it.

    Built on every view from the stored parse - the segments the comparison reads - so it cannot
    disagree with the checks and nothing is stored a second time. A parse that is gone or will
    not load is a notice, not a 500. Showing the content is reading the document, so it is
    audited the way a rendered page image is.
    """
    doc = job.document
    view: reading.Reading | None = None
    notes: list[str] = []
    scanned = False
    if job.status == JobStatus.done and doc.status == DocumentStatus.ready:
        try:
            segments = load_segments(settings, doc.id, doc.kind, notes=notes)
            scanned = is_scanned_pdf(settings, doc.id, doc.kind)
            view = reading.build(segments, part, max_items=settings.compare_max_segments)
        except Exception as e:  # a stored parse can be missing or damaged: the page says so
            log.warning(
                "reading_unavailable", job_id=job.id, document_id=doc.id, error=type(e).__name__
            )
            if isinstance(e, read_quality.StoredParseError):
                fail_unreadable(doc.id)
            view, notes, scanned = None, [], False
        else:
            if (
                refused := _audited(
                    request,
                    principal,
                    session,
                    action="document.content_view",
                    document_id=doc.id,
                    job_id=job.id,
                    detail=f"part {view.part} of {view.parts}",
                )
            ) is not None:
                return refused
    earlier = sorted(
        (j for j in doc.jobs if j.kind == "extract" and j.status == JobStatus.done),
        key=lambda j: j.created_at,
        reverse=True,
    )[:5]
    return _render(
        request,
        "parse.html",
        user=principal.user,
        job=job,
        doc=doc,
        view=view,
        notes=notes,
        scanned=scanned,
        earlier=earlier,
    )


def _report_job_url(session: Session, job: Job) -> str:
    """Where a model job's answer is read: the report it was queued from.

    Both kinds hang off one document and carry the other in ``params``. An act review does not
    keep the contractor type or the advance the report was opened with - the job never needed
    them - so those come from the recorded check when there is one, and are otherwise left out:
    the act is then read as the rules alone read it, with the review applied.
    """
    params = job.params
    if job.kind == "compare_explain":
        left = params.get("left")
        if isinstance(left, str) and left:
            query = urlencode({"left": left, "right": job.document_id, "job": job.id})
            return f"/ui/checks/compare?{query}"
    elif job.kind == "aoa_review":
        contract = params.get("contract")
        documents = [(job.document_id, "act")]
        if isinstance(contract, str) and contract:
            documents.append((contract, "contract"))
        run = history.find_run(session, job.owner, "aoa", documents)
        recorded = run.params if run is not None else {}
        query_params = {
            name: str(recorded[name]) for name in _RUN_OPTIONS["aoa"] if recorded.get(name)
        }
        query_params["act"] = job.document_id
        if len(documents) > 1:
            query_params["contract"] = documents[1][0]
        query_params["review"] = job.id
        return f"/ui/checks/aoa?{urlencode(query_params)}"
    return f"/ui/documents/{job.document_id}"


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
    contractor_type: Annotated[str | None, Form(max_length=32)] = None,
    advance_amount: Annotated[str | None, Form(max_length=40)] = None,
    advance_date: Annotated[str | None, Form(max_length=40)] = None,
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
            if len(out) >= limit:
                break  # the rest would be dropped anyway, and checking each against ``out`` grows
    return out


@router.get("/checks/aoa")
def aoa_report(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    act: str,
    contract: str | None = None,
    contractor_type: Annotated[str | None, Query(max_length=32)] = None,
    advance_amount: Annotated[str | None, Query(max_length=40)] = None,
    advance_date: Annotated[str | None, Query(max_length=40)] = None,
    review: str | None = None,
    registry: Annotated[str | None, Query(max_length=600)] = None,
) -> Response:
    """The rules' report, recomputed on every view. A finished model review rides in
    ``review`` and is applied to the notes the rules could not decide; its absence means
    nobody asked the model."""
    try:
        aoa_doc, contract_doc, ctype = _aoa_inputs(
            session, principal, act, contract, contractor_type
        )
        advance = parse_advance(advance_amount, advance_date)
        typed, typed_link = _typed_rates(request)
        extracts = _extracts(session, principal, registry)
        review_row = owned_review_job(session, principal, review, aoa_doc, contract_doc)
        report = run_aoa(
            settings,
            aoa_doc,
            contract_doc,
            ctype,
            advance,
            review_job_id=review_row.id if review_row is not None else None,
            entered_rates=typed,
            registry_docs=extracts,
        )
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/aoa")
    _remember_aoa(
        session,
        principal,
        aoa_doc,
        contract_doc,
        ctype,
        (advance_amount, advance_date),
        review_row,
        typed_link,
        extracts,
    )
    if (
        refused := _audited(
            request,
            principal,
            session,
            action="check.aoa.json",
            document_id=aoa_doc.id,
            detail=f"{len(report.errors)} error(s), {len(report.findings)} finding(s)",
        )
    ) is not None:
        return refused
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
        fx_days=_fx_days(report),
        registry_ids=",".join(d.id for d in extracts),
        # the model review: offered only where it can run, and only for notes left open
        can_review=settings.aoa_llm_enabled
        and get_llm(request) is not None
        and contract_doc is not None,
        review_items=int(report.summary.get("review_items", "0")),
        review_job=review_row.id if review_row is not None else None,
        review_status=review_status,
        review_running=review_status in (JobStatus.queued.value, JobStatus.running.value),
        review_state=report.summary.get("review"),
        # a review that failed because the model did not answer is asked again, not reused
        review_retry=review_row is not None
        and review_row.status == JobStatus.failed
        and (review_row.message or "").startswith(LLM_UNAVAILABLE),
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
    contractor_type: Annotated[str | None, Query(max_length=32)] = None,
    advance_amount: Annotated[str | None, Query(max_length=40)] = None,
    advance_date: Annotated[str | None, Query(max_length=40)] = None,
    review: str | None = None,
    registry: Annotated[str | None, Query(max_length=600)] = None,
) -> Response:
    """A plain link, not a JS blob: works with JavaScript off and needs no CSP argument. The
    report's own query rides along, so the Word file carries the model's reading when the page
    does."""
    return _aoa_file(
        request, principal, session, settings, act, contract, contractor_type,
        (advance_amount, advance_date), review, registry, fmt="docx",
    )  # fmt: skip


@router.get("/checks/aoa/annotated.pdf")
def aoa_annotated_pdf(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    act: str,
    contract: str | None = None,
    contractor_type: Annotated[str | None, Query(max_length=32)] = None,
    advance_amount: Annotated[str | None, Query(max_length=40)] = None,
    advance_date: Annotated[str | None, Query(max_length=40)] = None,
    review: str | None = None,
    registry: Annotated[str | None, Query(max_length=600)] = None,
) -> Response:
    """The same link for an act that arrived as a PDF: the PDF with a highlight and a note for
    each finding."""
    return _aoa_file(
        request, principal, session, settings, act, contract, contractor_type,
        (advance_amount, advance_date), review, registry, fmt="pdf",
    )  # fmt: skip


def _aoa_file(
    request: Request,
    principal: Principal,
    session: Session,
    settings: Settings,
    act: str,
    contract: str | None,
    contractor_type: str | None,
    advance_query: tuple[str | None, str | None],
    review: str | None,
    registry: str | None,
    *,
    fmt: Literal["docx", "pdf"],
) -> Response:
    """The act checked as the report shows it, as a file with the findings marked in it."""
    try:
        aoa_doc, contract_doc, ctype = _aoa_inputs(
            session, principal, act, contract, contractor_type
        )
        advance = parse_advance(*advance_query)
        review_row = owned_review_job(session, principal, review, aoa_doc, contract_doc)
        report = run_aoa(
            settings,
            aoa_doc,
            contract_doc,
            ctype,
            advance,
            review_job_id=review_row.id if review_row is not None else None,
            entered_rates=_typed_rates(request)[0],
            registry_docs=_extracts(session, principal, registry),
        )
        # 415 for a file of the other kind: an HTML navigation must not land on raw JSON.
        writer = annotated_docx if fmt == "docx" else annotated_pdf
        resp = writer(settings, aoa_doc, report)
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/aoa")
    if (
        refused := _audited(
            request,
            principal,
            session,
            action=f"check.aoa.{fmt}",
            document_id=aoa_doc.id,
            detail=f"{len(report.errors)} error(s), {len(report.findings)} finding(s)",
        )
    ) is not None:
        return refused
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
    contractor_type: Annotated[str | None, Query(max_length=32)] = None,
    advance_amount: Annotated[str | None, Query(max_length=40)] = None,
    advance_date: Annotated[str | None, Query(max_length=40)] = None,
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
    if typed_link := _typed_rates(request)[1]:
        params["rates"] = typed_link
    if registry_ids := ",".join(_ids(request.query_params.get("registry"), _MAX_EXTRACTS)):
        params["registry"] = registry_ids
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
        template_doc, bid_docs = _boq_inputs(session, principal, template, bids, settings)
    except ValidationError:
        return _error(request, principal, t("error.boq_inputs", _lang(request)))
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/boq")

    try:
        report, _, _, _ = run_boq(settings, template_doc, bid_docs)
    except HTTPException as e:  # a template over the cell limit: a page that says so
        return _http_error(request, principal, e, "/ui/start/boq")
    history.record_run(
        session,
        owner=principal.user,
        kind="boq",
        documents=[(template_doc.id, "template"), *((d.id, "bid") for d in bid_docs)],
        params={"template": template_doc.id, "bids": [d.id for d in bid_docs]},
    )
    if (
        refused := _audited(
            request,
            principal,
            session,
            action="check.boq.json",
            document_id=template_doc.id,
            detail=f"{len(bid_docs)} bidder(s)",
        )
    ) is not None:
        return refused
    # Red and green are decided by ``compare_all`` on the common-scope total - the positions
    # every bidder priced - so a bid that left sections out is still coloured, fairly, and ties
    # share a colour. The page only shows what was decided; it never ranks on its own.
    resp = _render(
        request,
        "check_boq.html",
        user=principal.user,
        report=report,
        query=request.url.query,
        lowest=[b for b in report.leaderboard if b.colour == "green"],
        highest=[b for b in report.leaderboard if b.colour == "red"],
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
        template_doc, bid_docs = _boq_inputs(session, principal, template, bids, settings)
    except ValidationError:
        return _error(request, principal, t("error.boq_inputs", _lang(request)))
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/boq")

    try:
        report, book, books, comparisons = run_boq(settings, template_doc, bid_docs)
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/start/boq")
    if (
        refused := _audited(
            request,
            principal,
            session,
            action="check.boq.xlsx",
            document_id=template_doc.id,
            detail=f"{len(bid_docs)} bidder(s)",
        )
    ) is not None:
        return refused
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
                "label_ka": delta.label_ka,
                "label_en": delta.label_en,
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

    history.record_run(
        session,
        owner=principal.user,
        kind="compare",
        documents=[(left_doc.id, "left"), (right_doc.id, "right")],
        params={"left": left_doc.id, "right": right_doc.id, **({"job": job} if job else {})},
        keep=("job",),
    )
    if (
        refused := _audited(
            request,
            principal,
            session,
            action="check.compare.json",
            document_id=right_doc.id,
            detail=f"{report.counts.total_changes} change(s), {len(report.errors)} critical",
        )
    ) is not None:
        return refused
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
        explain_failed=explain_row is not None and explain_row.status == JobStatus.failed,
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
        # The same pair asked about again - a double click, a reload of the page that posted -
        # is the same job, not another queued behind the first: the model pass is the one costly
        # thing on this screen, and the queue is a single thread.
        same_pair = session.scalars(
            select(Job).where(
                Job.owner == principal.user,
                Job.kind == "compare_explain",
                Job.document_id == right_doc.id,
                Job.status.in_((JobStatus.queued, JobStatus.running, JobStatus.done)),
            )
        ).all()
        existing = next(
            (
                j
                for j in same_pair
                if j.params.get("left") == left_doc.id
                and j.params.get("right") == right_doc.id
                and explain_answered(settings, right_doc.id, j)
            ),
            None,
        )
        if existing is not None:
            params = urlencode({"left": left_doc.id, "right": right_doc.id, "job": existing.id})
            return RedirectResponse(
                f"/ui/checks/compare?{params}", status_code=status.HTTP_303_SEE_OTHER
            )
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
    if (
        refused := _audited(
            request,
            principal,
            session,
            action="check.compare.docx",
            document_id=right_doc.id,
            detail=f"{len(report.findings)} finding(s)",
        )
    ) is not None:
        return refused
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
    if (
        refused := _audited(
            request,
            principal,
            session,
            action="check.compare.xlsx",
            document_id=right_doc.id,
            detail=f"{report.counts.total_changes} change(s)",
        )
    ) is not None:
        return refused
    return compare_xlsx(report)


# --- shared helpers ----------------------------------------------------------------------------


def _aoa_inputs(
    session: Session, principal: Principal, act: str, contract: str | None, ctype: str | None
) -> tuple[Document, Document | None, ContractorType | None]:
    aoa_doc = owned_document(session, principal, act)
    contract_doc = owned_document(session, principal, contract) if contract else None
    valid = ctype if ctype in get_args(ContractorType) else None
    return aoa_doc, contract_doc, cast("ContractorType | None", valid)


#: The most registry extracts one act check takes: a party each, and room for a few more.
_MAX_EXTRACTS = 6


def _extracts(session: Session, principal: Principal, raw: str | None) -> list[Document]:
    """The caller's uploaded registry extracts a report's link names (``registry=<ids>``)."""
    return [owned_document(session, principal, i) for i in _ids(raw, _MAX_EXTRACTS)]


def _typed_rates(request: Request) -> tuple[dict[tuple[str, date], Decimal], str]:
    """The exchange rates typed in for rule 9's invoice, from the link (``rates``) and from the
    report form's fields (``rate_day`` / ``rate_value`` pairs, which win), and the same rates
    written back as a link carries them - for the history and the next links. Lenient: what does
    not read is left out (``fx_invoice.parse_entered_rates``)."""
    q = request.query_params
    days = [d[:24] for d in q.getlist("rate_day")]
    values = [v[:24] for v in q.getlist("rate_value")]
    typed = parse_entered_rates((q.get("rates") or "")[:1200], days, values)
    return typed, format_entered_rates(typed)


def _fx_days(report: CheckReport) -> list[dict[str, str]]:
    """One entry per day whose rate the act's invoice takes, for the report form's rate fields:
    the day, the rate it got and from where (the imported NBG table, or typed in)."""
    s = report.summary
    currency = s.get("currency", "")
    out: list[dict[str, str]] = []
    for day in filter(None, s.get("fx_days", "").split(",")):
        out.append(
            {
                "key": f"{currency}:{day}",
                "day": day,
                "rate": s.get(f"fx_rate_{day}", ""),
                "source": s.get(f"fx_source_{day}", ""),
            }
        )
    return out


def _boq_inputs(
    session: Session, principal: Principal, template: str, bids: str, settings: Settings
) -> tuple[Document, list[Document]]:
    """Raises ``ValidationError`` for an empty bid list or an over-long template id - both
    reachable by editing the URL - which the callers turn into a page, not a 500."""
    body = BoqCheckRequest(template_document_id=template, bid_document_ids=_ids(bids, 10))
    return boq_documents(session, principal, body, settings)


def _expired(request: Request, principal: Principal, again: str) -> Response:
    """A retention sweep - or a link shared with a colleague - and the documents are gone.

    ``owned_document`` returns the same 404 for a deleted document and for one belonging to
    someone else, so this page says "no longer available to you" rather than claiming deletion.
    Without it the browser would render the raw JSON 404: the handler in ``main`` only
    special-cases 401.
    """
    return _render(request, "expired.html", user=principal.user, again=again)


def _audited(
    request: Request,
    principal: Principal,
    session: Session,
    *,
    action: str,
    document_id: str | None = None,
    job_id: str | None = None,
    detail: str | None = None,
) -> Response | None:
    """Record that a report or a read is about to be served, and commit the record first.

    None when it is stored. When it cannot be (a locked database, a full disk) the answer is a
    503 page instead of the report: a read of client documents that left no trace is not served.
    """
    try:
        audit_or_503(
            session,
            actor=principal.user,
            action=action,
            document_id=document_id,
            job_id=job_id,
            ip=client_ip(request),
            detail=detail,
        )
    except HTTPException as e:
        return _http_error(request, principal, e, "/ui/")
    return None


def _error(
    request: Request, principal: Principal, message: object, status_code: int = status.HTTP_200_OK
) -> Response:
    resp = _render(request, "error.html", user=principal.user, message=message)
    resp.status_code = status_code
    return resp


def _http_error(request: Request, principal: Principal, exc: HTTPException, again: str) -> Response:
    """Map an API refusal onto a page: 404 means the documents are gone (or never were the
    caller's), anything else - not ready yet, wrong file type, too many jobs - says why.

    A refusal with a code (``api.refusals``: a document that holds no text, one that cannot be
    read again) is said in the page's language, the code kept so it can be quoted, and the page
    carries the refusal's own status - it is not a success that happens to show a sentence."""
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return _expired(request, principal, again)
    detail: object = exc.detail
    if isinstance(detail, dict):
        text = detail["message_ka" if _lang(request) == "ka" else "message_en"]
        return _error(request, principal, f"{detail['code']}: {text}", exc.status_code)
    return _error(request, principal, exc.detail)


def _lang(request: Request) -> Lang:
    return lang_of(request.cookies.get(LANG_COOKIE))


#: What each report's URL may carry besides its documents. Anything else in a stored check is
#: ignored when its link is rebuilt, so a damaged row cannot put a stray parameter in a URL.
_RUN_OPTIONS: dict[str, tuple[str, ...]] = {
    "aoa": (
        "act",
        "contract",
        "contractor_type",
        "advance_amount",
        "advance_date",
        "rates",
        "registry",
        "review",
    ),
    "compare": ("left", "right", "job"),
}


def _remember_aoa(
    session: Session,
    principal: Principal,
    act: Document,
    contract: Document | None,
    ctype: str | None,
    advance: tuple[str | None, str | None],
    review: Job | None,
    rates: str = "",
    extracts: Sequence[Document] = (),
) -> None:
    """Record the act check that was just opened, with the options its URL carried."""
    params: dict[str, object] = {"act": act.id}
    documents = [(act.id, "act")]
    if contract is not None:
        params["contract"] = contract.id
        documents.append((contract.id, "contract"))
    if ctype:
        params["contractor_type"] = ctype
    for name, value in zip(("advance_amount", "advance_date"), advance, strict=True):
        if value and value.strip():
            params[name] = value.strip()[:64]  # as typed: the report reads it leniently
    if rates:
        params["rates"] = rates  # already read and written back (``_typed_rates``)
    if extracts:
        params["registry"] = ",".join(d.id for d in extracts)
        documents.extend((d.id, "registry") for d in extracts)
    if review is not None:
        params["review"] = review.id
    history.record_run(
        session,
        owner=principal.user,
        kind="aoa",
        documents=documents,
        params=params,
        keep=("review",),
    )


def _report_url(run: CheckRun) -> str:
    """The link that opens a recorded check again: its own route, its documents, its options."""
    params = run.params
    if run.kind == "boq":
        bids = params.get("bids")
        query = {
            "template": str(params.get("template", "")),
            "bids": ",".join(str(b) for b in bids) if isinstance(bids, list) else "",
        }
    else:
        query = {
            name: str(params[name]) for name in _RUN_OPTIONS.get(run.kind, ()) if params.get(name)
        }
    return f"/ui/checks/{run.kind}?{urlencode(query)}"


def _checks_for(session: Session, principal: Principal, doc: Document) -> list[dict[str, object]]:
    """The checks this file took part in, as the document page lists them."""
    out: list[dict[str, object]] = []
    for run in history.runs_for_document(session, principal.user, doc.id):
        if run.kind not in ("aoa", "boq", "compare"):
            continue
        role = next((p.role for p in run.documents if p.document_id == doc.id), "")
        others = [p.document.display_name for p in run.documents if p.document_id != doc.id]
        out.append(
            {
                "kind": run.kind,
                "role": role,
                "others": others[:3],
                "more": max(len(others) - 3, 0),
                "opened": run.last_opened_at,
                "url": _report_url(run),
            }
        )
    return out


def _by_severity(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    order = ("error", "warning", "info")
    groups = [(s, [f for f in findings if f.severity == s]) for s in order]
    return [(s, items) for s, items in groups if items]

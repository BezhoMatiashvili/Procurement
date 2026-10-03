"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from procurement_agent.api import checks, documents, extraction, health
from procurement_agent.config import SMALL_BODY_BYTES, Settings, get_settings
from procurement_agent.db import checkpoint, init_db
from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.ollama_client import OllamaClient
from procurement_agent.security import egress
from procurement_agent.security.audit import configure_logging, log, report_blocked_egress
from procurement_agent.security.body_limit import BodySizeLimitMiddleware
from procurement_agent.security.hardening import harden_process
from procurement_agent.security.headers import COMMON, HSTS, PAGE_CSP
from procurement_agent.security.hosts import LOCAL_HOSTS, AllowedHostsMiddleware
from procurement_agent.security.preauth import PreAuthMiddleware, upload_routes
from procurement_agent.security.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    rate_limit_middleware,
)
from procurement_agent.ui.routes import router as ui_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.validate_for_prod()
    restore_process = harden_process()
    if settings.egress_guard:
        # Before the worker (and with it Docling and the LLM client) exists: from here on, this
        # process opens connections to loopback and to the model server, and to nothing else.
        model_host = urlparse(settings.ollama_base_url).hostname or ""
        guard = egress.ensure_installed([model_host], on_block=report_blocked_egress)
        if settings.use_parser_socket:
            # The one Unix address this process may connect to: the parser container's socket.
            guard.allow_unix(settings.parser_socket)
    settings.ensure_dirs()
    init_db(settings.db_path)
    llm = OllamaClient(settings)
    app.state.llm = llm
    worker = Worker(settings, llm)
    worker.start()
    app.state.worker = worker
    log.info("startup", env=settings.env, model=settings.llm_model)
    if settings.env == "prod" and not settings.use_parser_socket:
        log.warning("parser_inline_in_production", detail="parsing runs in the web process")
    try:
        yield
    finally:
        # The LLM client is closed only once the worker is known to be out of it. If the join
        # timed out, a job is mid-call: closing the client under it would fail that job with a
        # misleading transport error, and the process is exiting anyway - the OS closes the
        # sockets, and the next start's recovery marks the job failed.
        if worker.stop():
            llm.close()
        else:
            log.warning(
                "worker_still_running_at_shutdown", timeout_s=settings.worker_stop_timeout_s
            )
        checkpoint()  # what was deleted this run does not stay readable in the write-ahead log
        restore_process()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(json_output=settings.env == "prod")
    app = FastAPI(
        title="Procurement Agent",
        version="0.1.0",
        lifespan=_lifespan,
        # The interactive docs page loads its script from a public CDN: the one place this
        # application would send a browser off the machine (and the CSP would block it anyway).
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env == "dev" else None,
    )
    app.state.settings = settings
    app.state.llm = None
    app.state.worker = None

    app.include_router(health.router)
    app.include_router(checks.router)
    app.include_router(documents.router)
    app.include_router(extraction.router)
    app.include_router(ui_router)
    static_dir = Path(__file__).parent / "ui" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    uploads = upload_routes(app.routes)

    # The middleware stack, innermost first (the last one added is the first to see a request):
    #
    #   hosts -> body size -> security headers -> rate limit -> pre-auth -> router
    #
    # A request for a name this server is not listed under is refused before anything looks at
    # it. The body is counted next, on the bytes that arrive, against the cap of its route. The
    # rate limit then bounds how often anyone may ask - before a database lookup. And only a
    # request that gets past it has its credential checked, ahead of its body (``preauth``).
    app.add_middleware(PreAuthMiddleware, routes=uploads)
    limiter = RateLimiter(RateLimitConfig(settings.rate_limit))
    app.state.limiter = limiter
    app.middleware("http")(rate_limit_middleware(limiter))

    @app.exception_handler(StarletteHTTPException)
    async def _unauthorized_ui_goes_to_login(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        """A browser opening a UI page without a session belongs on the sign-in form.

        API clients still get the plain JSON 401 — only ``/ui/`` is redirected, and only for
        navigations, so a fetch() from app.js still sees the status code it polls for.
        """
        is_ui = request.url.path.startswith("/ui/")
        is_navigation = request.method in ("GET", "HEAD") and "text/html" in request.headers.get(
            "accept", ""
        )
        if exc.status_code == 401 and is_ui and is_navigation:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/ui/login?next={quote(target, safe='')}", status_code=303)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_without_input(request: Request, exc: RequestValidationError) -> Response:
        """FastAPI's default 422 echoes each offending value back (``input``): an over-long key
        typed at sign-in, a pasted paragraph in a form field. Where a field failed and why is all a
        client needs; what it sent, it already has."""
        errors = [
            {
                "loc": [str(part)[:64] for part in err.get("loc", ())],
                "msg": str(err.get("msg", ""))[:200],
                "type": str(err.get("type", ""))[:64],
            }
            for err in exc.errors()[:20]
        ]
        return JSONResponse({"detail": errors}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in COMMON.items():
            response.headers[name] = value
        response.headers["Content-Security-Policy"] = PAGE_CSP
        if settings.env == "prod":
            response.headers["Strict-Transport-Security"] = HSTS
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        elif request.url.path.startswith("/ui/"):
            # Every UI page shows a document list, a report or extracted values, and every UI
            # download is client data: none of it may outlive the session in a browser cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    # Counts the raw bytes before any other layer reads the body.
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_max_bytes=SMALL_BODY_BYTES,
        upload_max_bytes=settings.max_request_bytes,
        upload_routes=uploads,
    )

    always = (*LOCAL_HOSTS, "testserver") if settings.env == "dev" else LOCAL_HOSTS
    app.add_middleware(AllowedHostsMiddleware, allowed_hosts=settings.allowed_hosts, always=always)
    return app


app = create_app()

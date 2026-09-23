"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from procurement_agent.api import checks, documents, extraction, health
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import init_db
from procurement_agent.jobs.worker import Worker
from procurement_agent.llm.ollama_client import OllamaClient
from procurement_agent.security.audit import configure_logging, log
from procurement_agent.security.auth import COOKIE_NAME
from procurement_agent.security.body_limit import BodySizeLimitMiddleware
from procurement_agent.security.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    rate_limit_middleware,
)
from procurement_agent.ui.routes import router as ui_router

_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.validate_for_prod()
    settings.ensure_dirs()
    init_db(settings.db_path)
    llm = OllamaClient(settings)
    app.state.llm = llm
    worker = Worker(settings, llm)
    worker.start()
    app.state.worker = worker
    log.info("startup", env=settings.env, model=settings.llm_model)
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(json_output=settings.env == "prod")
    app = FastAPI(
        title="Procurement Agent",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs" if settings.env == "dev" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env == "dev" else None,
    )
    app.state.settings = settings
    app.state.llm = None
    app.state.worker = None

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

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        elif request.url.path.startswith("/ui/"):
            # Every UI page shows a document list, a report or extracted values, and every UI
            # download is client data: none of it may outlive the session in a browser cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    # Outermost, so it counts the raw bytes before any other layer reads the body.
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes, session_cookie=COOKIE_NAME
    )

    app.include_router(health.router)
    app.include_router(checks.router)
    app.include_router(documents.router)
    app.include_router(extraction.router)
    app.include_router(ui_router)
    static_dir = Path(__file__).parent / "ui" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()

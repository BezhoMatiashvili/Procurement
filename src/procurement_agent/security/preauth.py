"""Decide about a request before its body is read.

FastAPI parses a request body *before* it solves dependencies - the authentication dependency
included (``fastapi/routing.py`` reads ``form()``/``json()`` and only then ``solve_dependencies``).
So a caller with no credential, or a wrong one, used to make the server buffer or spool the whole
body first: up to the largest upload for a JSON post, and up to two files into the tmpfs for a
multipart one. The rate limit counts requests, not bytes.

Two pieces close that:

* ``upload_routes`` finds the routes that take a file, from the routes themselves. Only those may
  carry a large body; every other route is capped at ``SMALL_BODY_BYTES`` (``body_limit``).
  Discovery rather than a list means a new upload route is found automatically; and a discovery
  that finds nothing (a FastAPI internal changed) makes uploads fail loudly with 413, not open.
* ``PreAuthMiddleware`` runs the same ``authenticate`` the route will run - credential lookup,
  the same-origin check for cookie requests, the same audit of a refusal - for those routes
  *before* the body is touched, and answers 401/403 itself when it fails.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, params
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from procurement_agent.config import Settings
from procurement_agent.db import session_scope
from procurement_agent.security.auth import authenticate, cookie_name
from procurement_agent.security.context import request_ip
from procurement_agent.security.headers import error_reply

__all__ = ["PreAuthMiddleware", "UploadRoutes", "upload_routes"]

#: ``(METHOD, path)`` of every route that takes an uploaded file.
UploadRoutes = frozenset[tuple[str, str]]


def upload_routes(routes: Iterable[BaseRoute]) -> UploadRoutes:
    """Every ``(method, path)`` whose handler declares an ``UploadFile`` / ``File`` parameter."""
    found: set[tuple[str, str]] = set()
    for context in iter_route_contexts(list(routes)):
        route = context.original_route
        if not isinstance(route, APIRoute) or context.path is None:
            continue
        if any(isinstance(f.field_info, params.File) for f in route.dependant.body_params):
            found.update((m.upper(), context.path) for m in context.methods or ())
    return frozenset(found)


def _check(request: Request, settings: Settings) -> None:
    """The route's own ``authenticate``, called with the values FastAPI would have supplied."""
    with session_scope() as session:
        authenticate(
            request,
            key=request.headers.get("x-api-key"),
            cookie_token=request.cookies.get(cookie_name(settings)),
            session=session,
            settings=settings,
        )


class PreAuthMiddleware:
    """Pure ASGI. Sits inside the body-size and rate-limit layers and outside the router: a
    request that is over the limit, or over quota, never gets as far as a database lookup, and one
    that fails authentication never gets as far as its body."""

    def __init__(self, app: ASGIApp, *, routes: UploadRoutes) -> None:
        self.app = app
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        marker = request_ip.set(client[0] if client else None)
        try:
            await self._handle(scope, receive, send)
        finally:
            request_ip.reset(marker)

    async def _handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["method"], scope["path"]) not in self.routes:
            await self.app(scope, receive, send)
            return
        request = Request(scope)  # headers, cookies, client: its body is never touched here
        try:
            await run_in_threadpool(_check, request, scope["app"].state.settings)
        except HTTPException as refused:
            await error_reply(send, refused.status_code, str(refused.detail))
            return
        await self.app(scope, receive, send)

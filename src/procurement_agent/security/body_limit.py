"""Request-body cap, enforced on the bytes that actually arrive.

The previous check read ``Content-Length`` and trusted it. A chunked upload has no such header,
so it sailed past, and FastAPI spooled the whole multipart body to disk before any handler -
or the authentication dependency - ran. This middleware counts what ``receive`` delivers and
stops the request once the count passes the cap, whatever the headers claimed.

It also refuses a multipart body that carries no credential at all before reading it: body
parsing happens before dependencies are solved, so without this an anonymous client could make
the server spool up to the cap per request. A *wrong* credential still gets as far as the
normal 401 - telling valid from invalid needs the database, which this layer does not touch.
"""

from __future__ import annotations

import json

from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from procurement_agent.security.audit import log

_TOO_LARGE = "request body too large"


class BodySizeLimitMiddleware:
    """Pure ASGI, and outermost, so it sees ``receive`` before anything else wraps it.

    Once the count passes the cap the app is told the client disconnected - the one thing
    every layer above already knows how to stop reading on - and whatever the app then tries
    to answer is replaced by a 413. Raising from ``receive`` was the obvious alternative and
    does not work: ``BaseHTTPMiddleware`` reads the body inside an anyio task group, so the
    exception arrives wrapped in an ``ExceptionGroup`` and FastAPI reports it as a 400
    "error parsing the body".
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, session_cookie: str) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.session_cookie = session_cookie

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan, websocket: nothing to count
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _reply(send, 413, _TOO_LARGE)
            return
        if headers.get("content-type", "").lower().startswith("multipart/") and not (
            headers.get("x-api-key")
            or self.session_cookie in cookie_parser(headers.get("cookie", ""))
        ):
            await _reply(send, 401, "missing API key")
            return

        received = 0
        exceeded = False
        started = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _reply(send, 413, _TOO_LARGE)
                return  # the app's own answer to a body it never fully saw is dropped
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
            # Layers above may treat the synthetic disconnect as an error; the 413 below is
            # the answer either way.
        if exceeded:
            log.warning("request_body_too_large", path=scope.get("path", ""), limit=self.max_bytes)
            if not started:
                await _reply(send, 413, _TOO_LARGE)


async def _reply(send: Send, code: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"x-content-type-options", b"nosniff"),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

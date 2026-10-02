"""Request-body cap, enforced on the bytes that actually arrive.

The previous check read ``Content-Length`` and trusted it. A chunked upload has no such header,
so it sailed past, and FastAPI spooled the whole multipart body to disk before any handler -
or the authentication dependency - ran. This middleware counts what ``receive`` delivers and
stops the request once the count passes the cap, whatever the headers claimed.

The cap depends on the route. Only the routes that take a file may carry a large body
(``upload_max_bytes``: two files and framing); everything else - sign-in, the JSON of a check, a
form of ids - is cut off at ``default_max_bytes``, a megabyte, so a 100 MB JSON post is refused
after the first megabyte instead of being buffered, parsed and then answered with a 401. Who may
send a large body is ``preauth``'s question and is answered before the body is read.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from procurement_agent.security.audit import log
from procurement_agent.security.headers import error_reply

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

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_max_bytes: int,
        upload_max_bytes: int,
        upload_routes: frozenset[tuple[str, str]],
    ) -> None:
        self.app = app
        self.default_max_bytes = default_max_bytes
        self.upload_max_bytes = upload_max_bytes
        self.upload_routes = upload_routes

    def limit_for(self, method: str, path: str) -> int:
        """The most bytes a request to this route may carry."""
        if (method, path) in self.upload_routes:
            return self.upload_max_bytes
        return self.default_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan, websocket: nothing to count
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["method"], scope["path"])
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await error_reply(send, 413, _TOO_LARGE)
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
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await error_reply(send, 413, _TOO_LARGE)
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
            log.warning("request_body_too_large", path=scope.get("path", ""), limit=limit)
            if not started:
                await error_reply(send, 413, _TOO_LARGE)

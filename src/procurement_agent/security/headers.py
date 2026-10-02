"""The response headers every answer of this application carries, whoever produced it.

Pages and API answers are produced inside the application and get them from the middleware in
``main``. The refusals written by the two outermost layers - a host that is not listed (``hosts``),
a body over its limit (``body_limit``) - never reach that middleware, so they used to carry none of
them: no CSP, no ``nosniff``. They send the same set through ``error_reply``.
"""

from __future__ import annotations

import json

from starlette.types import Send

__all__ = ["COMMON", "ERROR_CSP", "HSTS", "PAGE_CSP", "error_reply"]

#: What the application's own pages may load: itself, and nothing else.
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
#: An error body is text for a program or a person; nothing on it may load or be framed.
ERROR_CSP = "default-src 'none'; frame-ancestors 'none'"

#: Sent with every answer.
COMMON = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), "
        "bluetooth=(), accelerometer=(), gyroscope=(), magnetometer=(), interest-cohort=()"
    ),
    # No other site may keep a reference to a window of this one, or embed its responses.
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}
#: Only in production: it makes a browser refuse plain HTTP to this name for a year, which on a
#: development machine's ``localhost`` would break every other project served from there.
HSTS = "max-age=31536000; includeSubDomains"


async def error_reply(send: Send, code: int, detail: str, *, text: bool = False) -> None:
    """A complete answer written straight to the connection, for a request that is refused before
    its body is read (so the connection is closed rather than waited on)."""
    if text:
        body, content_type = detail.encode(), b"text/plain; charset=utf-8"
    else:
        body, content_type = json.dumps({"detail": detail}).encode(), b"application/json"
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"connection", b"close"),
        (b"content-security-policy", ERROR_CSP.encode()),
        *((k.lower().encode(), v.encode()) for k, v in COMMON.items()),
    ]
    await send({"type": "http.response.start", "status": code, "headers": headers})
    await send({"type": "http.response.body", "body": body})

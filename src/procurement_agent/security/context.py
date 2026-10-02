"""Per-request facts that deep code needs without being handed a ``Request``.

``api.deps.owned_document`` decides a document is not the caller's, and wants the refusal attributed
to an address; it is called from forty places that have a request and a session but pass neither on.
The pre-body layer (``preauth``) sees every request first and records the address here, in a
``ContextVar``: asyncio and the thread pool both copy the context into the work they start.
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ["request_ip"]

#: The client address of the request being handled, as uvicorn resolved it (it applies
#: ``X-Forwarded-For`` from trusted proxies only); None outside a request.
request_ip: ContextVar[str | None] = ContextVar("request_ip", default=None)

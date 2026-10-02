"""Which ``Host`` this server answers to.

Every URL a browser is sent to this application carries the name it used. Without a check, any
name that reaches the port is served: a DNS-rebinding page (a hostile site whose name is
re-pointed at 127.0.0.1 turns the visitor's browser into a client of this server, same-origin to
its own cookies), a tunnel somebody left running, a wrong name in a hosts file. The check is one
list, ``PA_ALLOWED_HOSTS``, plus the loopback names the health checks use.

Starlette's ``TrustedHostMiddleware`` splits the header at the first colon, which turns
``[::1]:8000`` into ``[``; and it takes the first of several ``Host`` headers, which a proxy and
this application can then read differently. This one parses the header whole and refuses a request
that carries two.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from procurement_agent.security.audit import log
from procurement_agent.security.headers import error_reply

__all__ = ["LOCAL_HOSTS", "AllowedHostsMiddleware", "host_of"]

#: The names this machine answers to. Always allowed: the container health checks and every
#: developer use them, and a request that names them has reached this machine by definition.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

_BAD_HOST_CHARS = frozenset(" \t\r\n/\\@#?")


def host_of(header: str) -> str | None:
    """The host name in a ``Host`` header value - lower case, no port, no brackets, no trailing
    dot - or ``None`` if the value is not a host (userinfo, a second colon, a path)."""
    value = header.strip().lower()
    if not value or any(c in _BAD_HOST_CHARS for c in value):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[1:end], value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        try:
            ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            return None
        return host
    host, colon, port = value.partition(":")
    if colon and not port.isdigit():
        return None
    return host.rstrip(".") or None


class AllowedHostsMiddleware:
    """Refuse (400) any request whose ``Host`` is not listed. Pure ASGI: it answers before a
    body is read, a session looked up or a handler run."""

    def __init__(
        self, app: ASGIApp, allowed_hosts: Iterable[str] = (), always: Iterable[str] = LOCAL_HOSTS
    ) -> None:
        self.app = app
        names = {h.strip().lower().rstrip(".") for h in (*allowed_hosts, *always) if h.strip()}
        self._exact = frozenset(n.strip("[]") for n in names if not n.startswith("*."))
        #: ``*.example.com`` matches ``a.example.com``, never ``example.com`` or ``badexample.com``.
        self._suffixes = tuple(n[1:] for n in names if n.startswith("*."))

    def allows(self, host: str) -> bool:
        if host in self._exact:
            return True
        try:
            if ipaddress.ip_address(host.split("%", 1)[0]).is_loopback:
                return True
        except ValueError:
            pass
        return any(host.endswith(s) and len(host) > len(s) for s in self._suffixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        values = [v for k, v in scope.get("headers", ()) if k == b"host"]
        host = host_of(values[0].decode("latin-1")) if len(values) == 1 else None
        if host is not None and self.allows(host):
            await self.app(scope, receive, send)
            return
        shown = "".join(c if c.isalnum() or c in ".-:[]_" else "?" for c in (host or ""))[:100]
        log.warning("host_refused", host=shown or None, headers=len(values))
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await error_reply(send, 400, "Invalid host header", text=True)

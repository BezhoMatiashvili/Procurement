"""A tripwire for outbound network use from this process.

The deployment promise is that nothing leaves the machine: the container's network has no route
out, and the Ollama URL has to name a private host. This module makes the same promise inside the
Python process, for every Python-level way of opening a connection - ``connect`` and ``connect_ex``,
``sendto``, and every name lookup (``getaddrinfo``, ``gethostbyname``) - which is where a library's
telemetry, a model download or an update check would come from (``requests``, ``urllib3``,
``httpx``, ``http.client``, ``huggingface_hub`` all end in those calls).

What is allowed: the loopback addresses, the names the process was told to talk to (the Ollama
server), together with the addresses those names resolve to, and the Unix socket paths it was told
of (``allow_unix``: the parser's, and no other). Everything else raises
``EgressBlockedError`` - an ``OSError``, so a library sees an ordinary network failure - and is
reported through ``on_block`` (content-free: what kind of call, which destination).

What it cannot see, and does not claim to: code that opens sockets outside Python's ``socket``
module (uvloop's event loop, native libraries). The kernel is the boundary for those - an internal
Docker network in deployment, ``unshare -rn`` in ``scripts/verify_offline.sh`` - and this is the
second line behind it, and the one that works on a developer's machine.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import threading
from collections.abc import Callable, Iterable
from typing import Any

__all__ = [
    "EgressBlockedError",
    "EgressGuard",
    "canonical_unix_path",
    "ensure_installed",
    "installed",
    "uninstall",
]

#: Names that always resolve to this machine.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


class EgressBlockedError(PermissionError):
    """A connection or a lookup that would have left this machine."""


def canonical_unix_path(path: str | bytes | os.PathLike[str]) -> str:
    """``path`` as text if it is an absolute path in canonical form, else ``ValueError``.

    Canonical: starts with a single ``/``, names a file, has no ``.`` or ``..`` segment, no ``//``,
    no trailing ``/``, no NUL byte - the string ``os.path.normpath`` would leave as it is. Anything
    else could mean one thing to a string comparison and another to the kernel.
    """
    try:
        text = os.fsdecode(path)
    except TypeError:
        text = ""
    if (
        not text.startswith("/")
        or text.startswith("//")
        or text.endswith("/")
        or "\0" in text
        or text != os.path.normpath(text)
    ):
        msg = (
            "a Unix socket path must be absolute and canonical: from the root, with no '.' or '..' "
            "segment, no '//' and no trailing '/'"
        )
        raise ValueError(msg)
    return text


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


class EgressGuard:
    """The rules, and the patches that apply them. One guard is installed per process."""

    def __init__(
        self,
        allowed_hosts: Iterable[str] = (),
        *,
        on_block: Callable[[str, str], None] | None = None,
        resolver: Callable[..., Any] | None = None,
    ) -> None:
        self._names: set[str] = set(_LOCAL_NAMES)
        self._ips: set[str] = set()
        self._unix: set[str] = set()
        self._on_block = on_block
        self._resolver = resolver
        self._lock = threading.Lock()
        self._originals: dict[str, Any] = {}
        self.blocked = 0
        for host in allowed_hosts:
            self.allow_host(host)

    # --- the rules -----------------------------------------------------------------------

    def allow_host(self, host: str) -> None:
        """Allow a name (and, once it is looked up, the addresses it resolves to) or an address."""
        if not host:
            return
        with self._lock:
            ip = _ip(host.strip("[]"))
            if ip is not None:  # a model server given by address: that address, nothing near it
                self._ips.add(str(ip))
            else:
                self._names.add(host.lower().rstrip("."))

    def allow_unix(self, path: str | bytes | os.PathLike[str]) -> None:
        """Allow connecting to this one Unix socket path - the parser's (``parser_service``).

        A pinned entry, not a family, and compared as the exact string the caller of ``connect``
        passes: the kernel resolves a path its own way (symlinks first, then ``..``), so a guard
        that tidied the path up before comparing would be a different parser from the one that
        decides where the connection goes. Hence only an absolute, already canonical spelling can be
        allowed (``canonical_unix_path``), and ``check_address`` accepts that spelling and no other.
        """
        key = canonical_unix_path(path)
        with self._lock:
            self._unix.add(key)

    @property
    def unix_paths(self) -> frozenset[str]:
        return frozenset(self._unix)

    @staticmethod
    def _unix_spelling(address: Any) -> str | None:
        """The address exactly as the kernel will be given it, or ``None`` for something that is
        not a path (a tuple, a number): never cleaned up, never made absolute."""
        if isinstance(address, bytearray | memoryview):
            address = bytes(address)
        try:
            return os.fsdecode(address)
        except TypeError:
            return None

    def _block(self, kind: str, destination: str) -> None:
        self.blocked += 1
        if self._on_block is not None:
            # reporting must never turn a refusal into another error
            with contextlib.suppress(Exception):
                self._on_block(kind, destination)
        msg = f"outbound {kind} to {destination!r} is not allowed: nothing leaves this machine"
        raise EgressBlockedError(msg)

    def _address_allowed(self, host: str) -> bool:
        ip = _ip(host)
        if ip is None:  # a name handed straight to connect(): only the names we were told about
            return host.lower().rstrip(".") in self._names
        return ip.is_loopback or str(ip) in self._ips

    def check_name(self, host: str | bytes | None, kind: str = "lookup") -> None:
        """Refuse a lookup of any name that is not ours; an address is judged as an address."""
        if host is None or host == "":
            return
        text = host.decode("idna") if isinstance(host, bytes) else str(host)
        if _ip(text) is not None:
            if not self._address_allowed(text):
                self._block(kind, text)
            return
        if text.lower().rstrip(".") not in self._names:
            self._block(kind, text)

    def check_address(self, family: int, address: Any, kind: str = "connect") -> None:
        if family == socket.AF_UNIX:
            if self._unix_spelling(address) not in self._unix:
                self._block(kind, "a unix socket")  # not the address: it is the caller's string
            return
        if family not in (socket.AF_INET, socket.AF_INET6):
            return  # socketpairs and the like stay on this machine
        host = address[0] if isinstance(address, tuple) else address
        text = host.decode("idna") if isinstance(host, bytes) else str(host)
        if not self._address_allowed(text.rstrip(".")):
            self._block(kind, text)

    @staticmethod
    def _is_bind_lookup(host: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        """``getaddrinfo(None | "0.0.0.0", port, flags=AI_PASSIVE)``: how a server asks for the
        address to bind. The wildcard is not loopback, but nothing is being reached. A *name* with
        the flag set is still a lookup and is judged as one."""
        flags = kwargs.get("flags", args[3] if len(args) > 3 else 0)
        if not flags & socket.AI_PASSIVE:
            return False
        if host is None or host == "":
            return True
        text = host.decode("idna") if isinstance(host, bytes) else str(host)
        return _ip(text) is not None

    def _remember(self, results: Iterable[Any]) -> None:
        with self._lock:
            for item in results:
                sockaddr = item[4]
                if sockaddr:
                    ip = _ip(str(sockaddr[0]))
                    if ip is not None:
                        self._ips.add(str(ip))

    # --- the patches ---------------------------------------------------------------------

    def install(self) -> None:
        if self._originals:
            return
        guard = self
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_sendto = socket.socket.sendto
        real_getaddrinfo = socket.getaddrinfo
        real_gethostbyname = socket.gethostbyname
        real_gethostbyname_ex = socket.gethostbyname_ex
        self._originals = {
            "connect": real_connect,
            "connect_ex": real_connect_ex,
            "sendto": real_sendto,
            "getaddrinfo": real_getaddrinfo,
            "gethostbyname": real_gethostbyname,
            "gethostbyname_ex": real_gethostbyname_ex,
        }
        resolve = self._resolver or real_getaddrinfo

        def connect(sock: socket.socket, address: Any) -> None:
            guard.check_address(sock.family, address, "connect")
            real_connect(sock, address)

        def connect_ex(sock: socket.socket, address: Any) -> int:
            guard.check_address(sock.family, address, "connect")
            return real_connect_ex(sock, address)

        def sendto(sock: socket.socket, data: Any, *args: Any) -> int:
            if args:  # sendto(data, address) or sendto(data, flags, address)
                guard.check_address(sock.family, args[-1], "send")
            return real_sendto(sock, data, *args)

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if not guard._is_bind_lookup(host, args, kwargs):
                guard.check_name(host)
            results = resolve(host, *args, **kwargs)
            guard._remember(results)
            return results

        def gethostbyname(host: str) -> str:
            guard.check_name(host)
            return real_gethostbyname(host)

        def gethostbyname_ex(host: str) -> Any:
            guard.check_name(host)
            return real_gethostbyname_ex(host)

        socket.socket.connect = connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
        socket.socket.sendto = sendto  # type: ignore[method-assign,assignment]
        socket.getaddrinfo = getaddrinfo
        socket.gethostbyname = gethostbyname
        socket.gethostbyname_ex = gethostbyname_ex

    def uninstall(self) -> None:
        if not self._originals:
            return
        socket.socket.connect = self._originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._originals["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = self._originals["sendto"]  # type: ignore[method-assign]
        socket.getaddrinfo = self._originals["getaddrinfo"]
        socket.gethostbyname = self._originals["gethostbyname"]
        socket.gethostbyname_ex = self._originals["gethostbyname_ex"]
        self._originals = {}

    @property
    def active(self) -> bool:
        return bool(self._originals)


_guard: EgressGuard | None = None


def ensure_installed(
    allowed_hosts: Iterable[str] = (), *, on_block: Callable[[str, str], None] | None = None
) -> EgressGuard:
    """The process's one guard, installed on first use; later calls add the hosts they name.

    A second application object in the same process (every test builds one) must not stack a
    second set of patches on the first, so it joins the guard that is already there.
    """
    global _guard
    if _guard is None or not _guard.active:
        _guard = EgressGuard(allowed_hosts, on_block=on_block)
        _guard.install()
        return _guard
    for host in allowed_hosts:
        _guard.allow_host(host)
    if on_block is not None:
        _guard._on_block = on_block
    return _guard


def installed() -> EgressGuard | None:
    return _guard if _guard is not None and _guard.active else None


def uninstall() -> None:
    global _guard
    if _guard is not None:
        _guard.uninstall()
    _guard = None

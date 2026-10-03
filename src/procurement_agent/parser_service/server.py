"""The parser service: converts one document at a time, on a Unix socket, and can be killed.

It runs in a container with no network, no database, no secrets and none of the application's
data (``docker-compose.yml``, service ``parser``); the web process hands it a document over the
socket and gets ``docling.json`` back (``procurement_agent/ingestion/parser_client.py``). What this
module guarantees, whichever converter it is given:

* **One request at a time.** The service is single-threaded: a request is read, converted and
  answered before the next connection is accepted. Another connection waits in the socket's backlog.
* **Nothing is trusted before it is checked.** The frame is validated against the caps and the
  allow-list of ``protocol`` before a payload is read; the document goes to a private file in a
  private directory (mode 0700 / 0600) that is removed whatever happens.
* **Errors are codes.** A failure is one of ``protocol.ErrorCode`` and nothing else crosses the
  socket: an exception message from a parser quotes the document it was parsing. The log gets the
  exception's *kind* (``safe_exc``), and a field of numbers.
* **A wedge ends in a kill.** Docling's own timeout is cooperative: it is checked between steps, and
  a native parser stuck inside a page never reaches the check - holding the GIL, so no Python code
  in this process, a watchdog thread included, can run either. So the conversion is armed with
  ``signal.alarm`` and ``SIGALRM`` is left at its *default* action: no Python handler stands between
  the signal and the kernel, which ends the process wherever it is. A soft ``RLIMIT_CPU`` is the
  backstop for a process that spins in a way the wall clock would also catch. Docker's
  ``restart: unless-stopped`` starts the container again (the models load again, which takes about a
  minute), and the web process, whose parse job was waiting on the socket, sees the connection
  close and fails that one job. The container runs under ``init: true``: a default-action signal is
  ignored by a process that is PID 1 of its namespace, and this one must not be.

``serve`` takes the converter as an argument, so the tests drive everything above with a fake one;
the real one is ``docling_converter``. ``KillSwitch`` is the guard ``main`` passes; the tests that
serve from a thread pass none, since a default-action signal ends the whole test process.
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

from procurement_agent.parser_service import protocol as p
from procurement_agent.security.safe_errors import safe_exc

if TYPE_CHECKING:
    from procurement_agent.config import Settings

__all__ = [
    "Converted",
    "Converter",
    "Guard",
    "KillSwitch",
    "Limits",
    "ping",
    "serve",
]

log = structlog.get_logger("procurement_agent.parser")

#: How often the accept loop looks at the stop event.
_ACCEPT_POLL_S = 0.2
#: How long a refused request's remaining bytes are read and thrown away before the connection is
#: closed (closing on unread data resets the connection and can discard the error reply).
_DRAIN_S = 1.0
#: ``sun_path`` of a Unix socket address on Linux, without its NUL.
_UNIX_PATH_MAX = 107


@dataclass(frozen=True)
class Converted:
    """What a converter returns: the page count, Docling's status word, and ``docling.json``."""

    pages: int
    status: str
    document: bytes


#: ``(private file, kind) -> Converted``; raises on failure. The file is named ``<stem>.<kind>``.
Converter = Callable[[Path, str], Converted]


class Guard(Protocol):
    def arm(self, seconds: int, threads: int) -> None: ...

    def disarm(self) -> None: ...


@dataclass(frozen=True)
class Limits:
    max_request_bytes: int
    max_response_bytes: int
    max_pages: int
    #: Seconds a conversion may run before the guard ends the process; ``None``: never armed.
    deadline_s: int | None
    #: Threads the converter may spend CPU on at once (the soft CPU limit allows that many).
    cpu_threads: int = 1
    read_timeout_s: float = 30.0
    write_timeout_s: float = 60.0

    @classmethod
    def from_settings(cls, settings: Settings) -> Limits:
        """The limits the application configured, from the same settings the web process reads."""
        return cls(
            max_request_bytes=settings.parser_max_request_bytes,
            max_response_bytes=settings.parser_max_result_bytes,
            max_pages=settings.max_pages,
            deadline_s=p.kill_deadline_s(settings.docling_timeout_s, settings.parser_kill_grace_s),
            cpu_threads=settings.docling_num_threads,
        )


# --- the kill switch -------------------------------------------------------------------------


class KillSwitch:
    """``signal.alarm`` with ``SIGALRM`` at its default action, and a CPU limit behind it.

    ``arm`` refuses to run when anything has put a handler on ``SIGALRM``: a handler is Python
    code, which a wedged native call never lets run. ``install`` puts the defaults back (an
    ignored signal is inherited across ``exec``) and must be called from the main thread, once.
    """

    def __init__(self) -> None:
        self._limits_before: tuple[int, int] | None = None

    def install(self) -> None:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.signal(signal.SIGXCPU, signal.SIG_DFL)

    def arm(self, seconds: int, threads: int) -> None:
        if signal.getsignal(signal.SIGALRM) is not signal.SIG_DFL:
            msg = "SIGALRM does not have its default action: the kill would wait for Python"
            raise RuntimeError(msg)
        used = resource.getrusage(resource.RUSAGE_SELF)
        soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        self._limits_before = (soft, hard)
        wanted = int(used.ru_utime + used.ru_stime) + 1 + seconds * max(threads, 1)
        if hard != resource.RLIM_INFINITY:
            wanted = min(wanted, hard)
        resource.setrlimit(resource.RLIMIT_CPU, (wanted, hard))
        signal.alarm(seconds)

    def disarm(self) -> None:
        signal.alarm(0)
        if self._limits_before is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_CPU, self._limits_before)
            self._limits_before = None


# --- binding ---------------------------------------------------------------------------------


def _bind(path: Path) -> socket.socket:
    """A listening socket at ``path``, mode 0660, in a directory the world cannot enter.

    A socket left by a killed server is removed first; anything else already at ``path`` is not
    ours to delete.
    """
    if len(os.fsencode(path)) > _UNIX_PATH_MAX:
        msg = f"the socket path is too long for a Unix address ({_UNIX_PATH_MAX} bytes at most)"
        raise RuntimeError(msg)
    try:
        mode = path.parent.stat().st_mode
    except OSError:
        msg = "the directory of the socket does not exist"
        raise RuntimeError(msg) from None
    if not stat.S_ISDIR(mode) or mode & 0o007:
        msg = "the directory of the socket must be a directory the world cannot enter"
        raise RuntimeError(msg)
    try:
        existing = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing):
            msg = "something that is not a socket is at the socket's path; it is not deleted"
            raise RuntimeError(msg)
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o117)  # a socket is created 0777 less the umask: 0660
    try:
        listener.bind(os.fspath(path))
    except BaseException:
        listener.close()
        raise
    finally:
        os.umask(previous)
    listener.listen(4)
    return listener


# --- serving ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    convert_fn: Converter
    limits: Limits
    guard: Guard | None
    workdir: Path | None


def serve(
    *,
    convert_fn: Converter,
    socket_path: Path,
    limits: Limits,
    guard: Guard | None = None,
    stop: threading.Event | None = None,
    ready: threading.Event | None = None,
    workdir: Path | None = None,
) -> None:
    """Accept connections and answer them, one at a time, until ``stop`` is set.

    ``guard`` is armed around each conversion when ``limits.deadline_s`` is set; with a deadline and
    no guard given, the real ``KillSwitch`` is used. ``ready`` is set once the socket is listening.
    """
    stop = stop or threading.Event()
    if limits.deadline_s is not None and guard is None:
        guard = KillSwitch()
    context = _Context(convert_fn, limits, guard, workdir)
    listener = _bind(socket_path)
    try:
        listener.settimeout(_ACCEPT_POLL_S)
        log.info("parser_listening", deadline_s=limits.deadline_s, threads=limits.cpu_threads)
        if ready is not None:
            ready.set()
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn:
                _serve_connection(conn, context)
    finally:
        listener.close()
        with contextlib.suppress(OSError):
            socket_path.unlink()


def _serve_connection(conn: socket.socket, context: _Context) -> None:
    try:
        _handle(conn, context)
    except OSError as e:  # the peer left: nothing to answer
        log.info("parser_connection_lost", error=safe_exc(e))
    except Exception as e:  # a bug in the service must not end it
        log.error("parser_connection_failed", error=safe_exc(e))
        with contextlib.suppress(OSError):
            _answer(conn, context, p.error_response_header(p.ErrorCode.INTERNAL))


def _answer(conn: socket.socket, context: _Context, header: p.Header, payload: bytes = b"") -> None:
    p.send_frame(conn, header, payload, timeout=context.limits.write_timeout_s)


def _handle(conn: socket.socket, context: _Context) -> None:
    limits = context.limits
    try:
        header, payload = p.read_frame(
            conn,
            limit=lambda h: p.request_payload_limit(p.check_request(h), limits.max_request_bytes),
            max_payload=limits.max_request_bytes,
            deadline=time.monotonic() + limits.read_timeout_s,
        )
        request = p.check_request(header)
    except p.ConnectionClosedError:
        return
    except p.ProtocolError as e:
        log.info("parser_request_refused", reason=e.reason)
        _refuse(conn, context, e)
        return
    if request.op == "ping":
        _answer(conn, context, p.ping_response_header())
        return
    _convert(conn, context, request, payload)


def _refuse(conn: socket.socket, context: _Context, error: p.ProtocolError) -> None:
    """Answer a request that broke the rules, then throw away what the peer is still sending."""
    with contextlib.suppress(OSError):
        _answer(conn, context, p.error_response_header(error.code))
        conn.shutdown(socket.SHUT_WR)
        if not isinstance(error, p.FrameTimeoutError | p.TruncatedError):
            _drain(conn, context.limits.max_request_bytes)


def _drain(conn: socket.socket, limit: int) -> None:
    end = time.monotonic() + _DRAIN_S
    taken = 0
    while taken < limit:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        conn.settimeout(remaining)
        chunk = conn.recv(1 << 16)
        if not chunk:
            return
        taken += len(chunk)


def _convert(conn: socket.socket, context: _Context, request: p.Request, payload: bytes) -> None:
    limits = context.limits
    kind = request.kind or ""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="parse-", dir=context.workdir) as private:
        source = Path(private) / f"{request.stem or 'document'}.{kind}"
        fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
        result = _run_converter(context, source, kind)
    if isinstance(result, p.ErrorCode):
        _answer(conn, context, p.error_response_header(result))
        return
    if (
        type(result.pages) is not int
        or result.pages < 0
        or result.status not in p.STATUSES
        or not isinstance(result.document, bytes)
    ):
        _answer(conn, context, p.error_response_header(p.ErrorCode.INTERNAL))
        return
    if result.pages > limits.max_pages:
        _answer(conn, context, p.error_response_header(p.ErrorCode.TOO_MANY_PAGES))
        return
    if len(result.document) > limits.max_response_bytes:
        log.warning("parser_result_too_large", bytes_out=len(result.document))
        _answer(conn, context, p.error_response_header(p.ErrorCode.RESULT_TOO_LARGE))
        return
    header = p.convert_response_header(pages=result.pages, status=result.status)
    _answer(conn, context, header, result.document)
    log.info(
        "parser_converted",
        kind=kind,
        bytes_in=len(payload),
        bytes_out=len(result.document),
        pages=result.pages,
        status=result.status,
        seconds=round(time.monotonic() - started, 2),
    )


def _run_converter(context: _Context, source: Path, kind: str) -> Converted | p.ErrorCode:
    """The conversion, armed. The exception's message is dropped: only its kind is logged."""
    guard = context.guard if context.limits.deadline_s is not None else None
    if guard is not None and context.limits.deadline_s is not None:
        try:
            guard.arm(context.limits.deadline_s, context.limits.cpu_threads)
        except Exception as e:  # never convert without the kill switch
            log.error("parser_killswitch_failed", error=safe_exc(e))
            return p.ErrorCode.INTERNAL
    try:
        return context.convert_fn(source, kind)
    except Exception as e:
        log.warning("parser_convert_failed", kind=kind, error=safe_exc(e))
        return p.ErrorCode.CONVERT_FAILED
    finally:
        if guard is not None:
            guard.disarm()


# --- the health probe ------------------------------------------------------------------------


def ping(socket_path: Path, *, timeout_s: float = 10.0) -> bool:
    """Does a parser answer a ping on ``socket_path`` within ``timeout_s``? The container's health
    check (``python -m procurement_agent.parser_service --ping``). A parser in the middle of a
    conversion does not answer until it is done, so a long parse can make this False for a while."""
    deadline = time.monotonic() + timeout_s
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout_s)
            conn.connect(os.fspath(socket_path))
            p.send_frame(conn, {"op": "ping"}, timeout=timeout_s)
            header, _ = p.read_frame(conn, limit=lambda h: 0, max_payload=0, deadline=deadline)
            return p.check_response(header, "ping", max_pages=0).ok
    except (OSError, p.ProtocolError):
        return False

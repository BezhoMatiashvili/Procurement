"""The parser's wire format: one frame is a fixed prefix, a small JSON header and a raw payload.

::

    prefix   17 bytes, big-endian: b"PAPS", version (1 byte), header length (4), payload length (8)
    header   UTF-8 JSON, at most 4 KiB: one flat object of strings, integers and booleans
    payload  raw bytes: the document going in, ``docling.json`` coming out

The reader decides from the prefix and the header alone, before it reads - let alone keeps - a
payload: the magic and version, a header over 4 KiB, a payload over the cap it was given, and the
operation's own cap are all refused with the bytes still unread. Nothing in this module evaluates,
unpickles or imports what a peer sent, and every refusal is one of a fixed set of reasons: an error
that crossed the wire says *which rule* was broken, never what the peer wrote. (The project's
content-free rule: the parser's input is a client document, and an exception message quotes it.)

Standard library only, and no socket: the reader takes anything with ``recv`` and ``settimeout``,
so the whole format is tested on byte strings.

Operations: ``ping`` (no payload) and ``convert`` (``kind`` is ``pdf`` or ``docx``, the payload is
the file; ``stem`` optionally names the file, because Docling writes the file's name into its
output and the output should be what the web process would have made). A successful ``convert``
answers ``pages`` and ``status`` and carries the bytes ``save_document`` writes; a failure is
``{"ok": false, "code": <ErrorCode>}`` and nothing else.
"""

from __future__ import annotations

import json
import re
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

__all__ = [
    "KINDS",
    "MAGIC",
    "MAX_HEADER_BYTES",
    "PREFIX",
    "REASONS",
    "REPLY_SLACK_S",
    "STATUSES",
    "TICK_S",
    "VERSION",
    "ConnectionClosedError",
    "ErrorCode",
    "FrameTimeoutError",
    "FrameTooLargeError",
    "Header",
    "ProtocolError",
    "Request",
    "Response",
    "Stream",
    "TruncatedError",
    "check_request",
    "check_response",
    "convert_response_header",
    "encode_frame",
    "error_response_header",
    "kill_deadline_s",
    "parse_header",
    "ping_response_header",
    "read_frame",
    "reply_deadline_s",
    "request_payload_limit",
    "send_frame",
]

MAGIC = b"PAPS"
VERSION = 1
#: magic, version, header length, payload length - big-endian, no padding.
PREFIX = struct.Struct(">4sBIQ")
MAX_HEADER_BYTES = 4096
#: A read waits at most this long before it gives its caller a chance to show life (``tick``).
TICK_S = 5.0
#: How long past the parser's own kill the client keeps listening, so that a kill is reported as
#: the parser stopping and not as the client giving up first.
REPLY_SLACK_S = 10

KINDS = ("pdf", "docx")
#: What Docling's ``ConversionStatus`` can be for a document that was converted at all.
STATUSES = ("success", "partial_success")
#: What names the file the converter is given: it is the web process's document id.
STEM = re.compile(r"[A-Za-z0-9_-]{1,64}")

_MAX_KEYS = 8
_MAX_KEY_LENGTH = 32
_MAX_INT = 2**31

Scalar = str | int | bool
Header = dict[str, Scalar]


class ErrorCode(StrEnum):
    """Why the parser did not answer with a document. The only text an error carries."""

    CONVERT_FAILED = "CONVERT_FAILED"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    UNSUPPORTED = "UNSUPPORTED"
    BAD_REQUEST = "BAD_REQUEST"
    TOO_LARGE = "TOO_LARGE"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    INTERNAL = "INTERNAL"


#: Every reason a frame is refused; ``str(ProtocolError)`` is always one of these.
REASONS = frozenset(
    {
        "bad_magic",
        "unsupported_version",
        "header_size",
        "header_encoding",
        "header_json",
        "header_shape",
        "payload_too_large",
        "truncated",
        "closed",
        "timeout",
        "unknown_operation",
        "unknown_kind",
        "bad_stem",
        "bad_request",
        "bad_response",
    }
)


class ProtocolError(Exception):
    """A frame that breaks the format. ``code`` is what a server answers with."""

    def __init__(self, reason: str, code: ErrorCode = ErrorCode.BAD_REQUEST) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class FrameTooLargeError(ProtocolError):
    def __init__(self) -> None:
        super().__init__("payload_too_large", ErrorCode.TOO_LARGE)


class FrameTimeoutError(ProtocolError):
    def __init__(self) -> None:
        super().__init__("timeout")


class TruncatedError(ProtocolError):
    def __init__(self) -> None:
        super().__init__("truncated")


class ConnectionClosedError(ProtocolError):
    """The peer closed before it sent the first byte of a frame: no frame, nothing wrong."""

    def __init__(self) -> None:
        super().__init__("closed")


class Stream(Protocol):
    """What the reader needs of a socket."""

    def recv(self, bufsize: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def settimeout(self, value: float | None, /) -> None: ...


# --- the header ------------------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            msg = "duplicate key"
            raise ValueError(msg)
        out[key] = value
    return out


def _refuse(_: str) -> Any:
    msg = "numbers other than integers are not part of the format"
    raise ValueError(msg)


def parse_header(raw: bytes) -> Header:
    """The header's JSON as a flat dict of strings, integers and booleans, or ``ProtocolError``.

    Strict on purpose: UTF-8 or nothing, no duplicate keys, no ``NaN``, no floats, no nesting, no
    ``null``; a boolean is a boolean and not a number. What the keys may be is the operation's
    business (``check_request``, ``check_response``).
    """
    if len(raw) > MAX_HEADER_BYTES:
        raise ProtocolError("header_size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("header_encoding") from None
    try:
        loaded = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_float=_refuse, parse_constant=_refuse
        )
    except (ValueError, RecursionError):
        raise ProtocolError("header_json") from None
    if type(loaded) is not dict or not 0 < len(loaded) <= _MAX_KEYS:
        raise ProtocolError("header_shape")
    for key, value in loaded.items():
        kind = type(value)
        if len(key) > _MAX_KEY_LENGTH or kind not in (str, int, bool):
            raise ProtocolError("header_shape")
        if kind is int and abs(value) >= _MAX_INT:
            raise ProtocolError("header_shape")
    return loaded


# --- frames ----------------------------------------------------------------------------------


def encode_frame(header: Mapping[str, Scalar], payload: bytes = b"") -> bytes:
    """A frame as bytes. A header the reader would refuse is refused here, not on the far side."""
    raw = _encode_header(header)
    return PREFIX.pack(MAGIC, VERSION, len(raw), len(payload)) + raw + payload


def _encode_header(header: Mapping[str, Scalar]) -> bytes:
    raw = json.dumps(dict(header)).encode("ascii")
    try:
        parse_header(raw)
    except ProtocolError:
        msg = "header outside what the format allows"
        raise ValueError(msg) from None
    return raw


def send_frame(
    conn: Stream,
    header: Mapping[str, Scalar],
    payload: bytes = b"",
    *,
    timeout: float | None = None,
) -> None:
    """Write one frame. The payload is sent as it is, not joined to the header (it can be 50 MB)."""
    raw = _encode_header(header)
    if timeout is not None:
        conn.settimeout(timeout)
    conn.sendall(PREFIX.pack(MAGIC, VERSION, len(raw), len(payload)) + raw)
    if payload:
        conn.sendall(payload)


def _read_exact(
    conn: Stream,
    count: int,
    *,
    deadline: float,
    clock: Callable[[], float],
    tick: Callable[[], None] | None,
    first: bool = False,
) -> bytes:
    """Exactly ``count`` bytes, read in slices of at most ``TICK_S`` seconds until ``deadline``.

    Nothing is allocated for bytes that have not arrived: the buffer grows with what was read, and
    ``count`` has been checked against a cap by then.
    """
    received = bytearray()
    while len(received) < count:
        remaining = deadline - clock()
        if remaining <= 0:
            raise FrameTimeoutError
        conn.settimeout(min(remaining, TICK_S))
        try:
            part = conn.recv(min(count - len(received), 1 << 20))
        except TimeoutError:
            if tick is not None:
                tick()
            continue
        if not part:
            raise ConnectionClosedError if first and not received else TruncatedError
        received += part
    return bytes(received)


def read_frame(
    conn: Stream,
    *,
    limit: Callable[[Header], int],
    max_payload: int,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    tick: Callable[[], None] | None = None,
) -> tuple[Header, bytes]:
    """One frame: its header and its payload, or the reason it was refused.

    ``max_payload`` is checked against the prefix, before anything else is read; ``limit`` is asked
    for the cap of *this* header's operation (it may refuse the header itself) before the payload is
    read. ``deadline`` is a ``clock()`` reading; ``tick`` is called whenever a slice of ``TICK_S``
    passes without data, so a caller waiting on a long conversion can show it is alive.
    """

    def exactly(count: int, *, first: bool = False) -> bytes:
        return _read_exact(conn, count, deadline=deadline, clock=clock, tick=tick, first=first)

    magic, version, header_length, payload_length = PREFIX.unpack(exactly(PREFIX.size, first=True))
    if magic != MAGIC:
        raise ProtocolError("bad_magic")
    if version != VERSION:
        raise ProtocolError("unsupported_version", ErrorCode.UNSUPPORTED)
    if not 0 < header_length <= MAX_HEADER_BYTES:
        raise ProtocolError("header_size")
    if payload_length > max_payload:
        raise FrameTooLargeError
    header = parse_header(exactly(header_length))
    if payload_length > limit(header):
        raise FrameTooLargeError
    return header, exactly(payload_length) if payload_length else b""


# --- requests --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    op: str
    kind: str | None = None
    stem: str | None = None


def check_request(header: Mapping[str, Scalar]) -> Request:
    """The request a header makes, or the ``ProtocolError`` (with the code to answer) it earns.

    An allow-list: exactly the keys of one known operation, each of its type, and a value the
    server knows. A request that is well formed but asks for something the server does not do
    (another operation, another kind of document) is ``UNSUPPORTED``; one that is not well formed
    is ``BAD_REQUEST``.
    """
    op = header.get("op")
    if type(op) is not str:
        raise ProtocolError("bad_request")
    if op == "ping":
        if set(header) != {"op"}:
            raise ProtocolError("bad_request")
        return Request(op="ping")
    if op != "convert":
        raise ProtocolError("unknown_operation", ErrorCode.UNSUPPORTED)
    keys = set(header)
    if not {"op", "kind"} <= keys <= {"op", "kind", "stem"}:
        raise ProtocolError("bad_request")
    kind = header["kind"]
    if type(kind) is not str:
        raise ProtocolError("bad_request")
    if kind not in KINDS:
        raise ProtocolError("unknown_kind", ErrorCode.UNSUPPORTED)
    stem = header.get("stem")
    if "stem" in header and (type(stem) is not str or not STEM.fullmatch(stem)):
        raise ProtocolError("bad_stem")
    return Request(op="convert", kind=kind, stem=stem if isinstance(stem, str) else None)


def request_payload_limit(request: Request, max_request_bytes: int) -> int:
    """How many payload bytes a request may carry: none for a ping, a file for a conversion."""
    return 0 if request.op == "ping" else max_request_bytes


# --- responses -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    ok: bool
    code: ErrorCode | None = None
    pages: int | None = None
    status: str | None = None


def ping_response_header() -> Header:
    return {"ok": True, "op": "ping"}


def convert_response_header(*, pages: int, status: str) -> Header:
    return {"ok": True, "op": "convert", "pages": pages, "status": status}


def error_response_header(code: ErrorCode) -> Header:
    return {"ok": False, "code": code.value}


def check_response(header: Mapping[str, Scalar], op: str, *, max_pages: int) -> Response:
    """What the parser said to ``op``, or ``ProtocolError``: the answer is untrusted input.

    Exactly the keys of the success or the error shape, of the right types; a status from the fixed
    set; a page count in range; an error code from ``ErrorCode``. Nothing else is accepted and
    nothing of it is echoed.
    """
    ok = header.get("ok")
    if type(ok) is not bool:
        raise ProtocolError("bad_response")
    if not ok:
        code = header.get("code")
        if set(header) != {"ok", "code"} or type(code) is not str:
            raise ProtocolError("bad_response")
        try:
            return Response(ok=False, code=ErrorCode(code))
        except ValueError:
            raise ProtocolError("bad_response") from None
    if header.get("op") != op:
        raise ProtocolError("bad_response")
    if op == "ping":
        if set(header) != {"ok", "op"}:
            raise ProtocolError("bad_response")
        return Response(ok=True)
    pages, status = header.get("pages"), header.get("status")
    if (
        op != "convert"
        or set(header) != {"ok", "op", "pages", "status"}
        or type(pages) is not int
        or not 0 <= pages <= max_pages
        or type(status) is not str
        or status not in STATUSES
    ):
        raise ProtocolError("bad_response")
    return Response(ok=True, pages=pages, status=status)


# --- deadlines -------------------------------------------------------------------------------


def kill_deadline_s(docling_timeout_s: int, grace_s: int) -> int:
    """Seconds a conversion may run before the kernel kills the parser: Docling's own timeout (a
    soft stop between steps, after which it still saves what it has) plus a grace for that."""
    return docling_timeout_s + grace_s


def reply_deadline_s(docling_timeout_s: int, grace_s: int) -> int:
    """Seconds the client waits for the answer. Always past the kill, so a conversion the parser
    was killed for is reported as the parser stopping and not as the client losing patience."""
    return kill_deadline_s(docling_timeout_s, grace_s) + REPLY_SLACK_S

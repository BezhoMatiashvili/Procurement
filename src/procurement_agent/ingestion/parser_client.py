"""The web process's side of the parser socket: send a document, store what comes back.

In ``socket`` mode (``PA_PARSER_MODE=socket``) a parse job does not run Docling; it sends the upload
to the parser container (``procurement_agent/parser_service``) and stores the ``docling.json`` the
parser returns, where ``docling_service.save_document`` would have written it. Everything after that
- the page count, the load with ``load_document``, the chunker - is untouched.

The rules this module keeps:

* **One try.** The connection is retried while the parser is not there yet (it is restarting, and
  loading its models takes about a minute), and *never* once a request has been sent: a document
  that kills the parser is sent once. The job fails, the parser restarts, the next job is served.
* **The answer is untrusted.** Header keys and types are checked against the allow-list of
  ``protocol.check_response``; the payload is read against ``parser_max_result_mb`` (the cap is
  decided on the prefix, before anything is allocated), must be JSON, a Docling document, with the
  pages its header says - and only then is it written.
* **Failures are codes.** Every failure is a ``ParserError`` whose message is a fixed sentence under
  a fixed code (``PARSER_UNAVAILABLE``, ``PARSER_TIMEOUT``, ``PARSER_CRASHED``, ...): nothing the
  parser said, and nothing from an exception, because both can quote the document. A
  ``ParserError`` is a ``ConversionError``, so the worker fails the job exactly as it fails any
  other conversion that did not work, and does not retry it.
* **The only socket is the parser's.** ``AF_UNIX``, the one configured path, which the
  application's egress guard allows by name (``main.py``) and nothing else.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from procurement_agent.config import Settings
from procurement_agent.ingestion import docling_service
from procurement_agent.ingestion.docling_service import ConversionError
from procurement_agent.parser_service import protocol as p

__all__ = ["ParserCode", "ParserError", "convert_through_parser", "default_reply_timeout_s"]

#: One attempt to connect waits this long (a parser that is busy converting still accepts into its
#: backlog, so this is rarely reached).
_CONNECT_TIMEOUT_S = 5.0
#: Between two attempts to connect.
_CONNECT_RETRY_S = 0.5


class ParserCode(StrEnum):
    PARSER_UNAVAILABLE = "PARSER_UNAVAILABLE"
    PARSER_TIMEOUT = "PARSER_TIMEOUT"
    PARSER_CRASHED = "PARSER_CRASHED"
    PARSER_PROTOCOL = "PARSER_PROTOCOL"
    PARSER_CONVERT_FAILED = "PARSER_CONVERT_FAILED"
    PARSER_TOO_MANY_PAGES = "PARSER_TOO_MANY_PAGES"
    PARSER_UNSUPPORTED = "PARSER_UNSUPPORTED"
    PARSER_BAD_REQUEST = "PARSER_BAD_REQUEST"
    PARSER_TOO_LARGE = "PARSER_TOO_LARGE"
    PARSER_RESULT_TOO_LARGE = "PARSER_RESULT_TOO_LARGE"
    PARSER_INTERNAL = "PARSER_INTERNAL"


_MESSAGES = {
    ParserCode.PARSER_UNAVAILABLE: "the parser service could not be reached",
    ParserCode.PARSER_TIMEOUT: "the parser service did not answer in time",
    ParserCode.PARSER_CRASHED: "the parser service stopped while reading the document",
    ParserCode.PARSER_PROTOCOL: "the parser service answered something that is not an answer",
    ParserCode.PARSER_CONVERT_FAILED: "the parser service could not convert the document",
    ParserCode.PARSER_TOO_MANY_PAGES: "the document has more pages than the limit",
    ParserCode.PARSER_UNSUPPORTED: "the parser service does not convert this kind of document",
    ParserCode.PARSER_BAD_REQUEST: "the parser service did not accept the request",
    ParserCode.PARSER_TOO_LARGE: "the document is larger than the parser service accepts",
    ParserCode.PARSER_RESULT_TOO_LARGE: "the converted document is larger than the limit",
    ParserCode.PARSER_INTERNAL: "the parser service failed",
}


#: What the parser's own error codes are called here.
_FROM_PARSER = {code: ParserCode(f"PARSER_{code.value}") for code in p.ErrorCode}


class ParserError(ConversionError):
    """A parse that did not work because of the parser service, as a code and a fixed sentence."""

    def __init__(self, code: ParserCode) -> None:
        super().__init__(f"{code.value}: {_MESSAGES[code]}")
        self.code = code


def default_reply_timeout_s(settings: Settings) -> float:
    """How long to wait for the answer: past the parser's kill (``protocol.reply_deadline_s``)."""
    return float(p.reply_deadline_s(settings.docling_timeout_s, settings.parser_kill_grace_s))


def _connect(
    settings: Settings,
    beat: Callable[[], None] | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> socket.socket:
    """A connection to the parser, waiting up to ``parser_connect_wait_s`` for it to be there.

    A missing socket file, a refused connection (the file a killed parser leaves behind) and a
    connect that times out are "not yet". Anything else - above all the egress guard refusing the
    path - is final.
    """
    path = os.fspath(settings.parser_socket)
    end = clock() + settings.parser_connect_wait_s
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(_CONNECT_TIMEOUT_S)
        try:
            conn.connect(path)
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as e:
            conn.close()
            if clock() >= end:
                raise ParserError(ParserCode.PARSER_UNAVAILABLE) from e
            if beat is not None:
                beat()
            sleep(_CONNECT_RETRY_S)
        except OSError as e:
            conn.close()
            raise ParserError(ParserCode.PARSER_UNAVAILABLE) from e
        else:
            return conn


def _exchange(
    conn: socket.socket,
    header: p.Header,
    data: bytes,
    settings: Settings,
    timeout_s: float,
    beat: Callable[[], None] | None,
) -> tuple[p.Response, bytes]:
    """Send the one request and read the one answer. Nothing here connects again."""
    try:
        p.send_frame(conn, header, data, timeout=timeout_s)
    except TimeoutError as e:
        raise ParserError(ParserCode.PARSER_TIMEOUT) from e
    except OSError:
        pass  # the parser may have refused the request and hung up; its answer is still readable
    cap = settings.parser_max_result_bytes
    headers: list[bool] = []  # whether each header read was a success; empty: none read yet

    def limit(h: p.Header) -> int:
        headers.append(h.get("ok") is True)
        return cap if headers[-1] else 0  # an error answer carries no payload

    try:
        answer, payload = p.read_frame(
            conn,
            limit=limit,
            max_payload=cap,
            deadline=time.monotonic() + timeout_s,
            tick=beat,
        )
        return p.check_response(answer, "convert", max_pages=settings.max_pages), payload
    except p.FrameTimeoutError as e:
        raise ParserError(ParserCode.PARSER_TIMEOUT) from e
    except p.FrameTooLargeError as e:
        # over the cap on the prefix, or a success answer over it: too large; an error answer that
        # carries anything at all is not an answer the parser gives
        too_large = ParserCode.PARSER_RESULT_TOO_LARGE
        raise ParserError(too_large if headers != [False] else ParserCode.PARSER_PROTOCOL) from e
    except (p.ConnectionClosedError, p.TruncatedError, OSError) as e:
        raise ParserError(ParserCode.PARSER_CRASHED) from e
    except p.ProtocolError as e:
        raise ParserError(ParserCode.PARSER_PROTOCOL) from e


def _checked_document(payload: bytes, pages: int | None) -> None:
    """The payload is JSON, a Docling document, and has the pages its header says."""
    try:
        document = json.loads(payload)
    except (ValueError, RecursionError) as e:
        raise ParserError(ParserCode.PARSER_PROTOCOL) from e
    listed = document.get("pages") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_name") != "DoclingDocument"
        or not isinstance(listed, dict)
        or len(listed) != pages
    ):
        raise ParserError(ParserCode.PARSER_PROTOCOL)


def convert_through_parser(
    path: Path,
    kind: str,
    out_dir: Path,
    settings: Settings,
    *,
    beat: Callable[[], None] | None = None,
    reply_timeout_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Convert the upload at ``path`` in the parser container and store ``docling.json`` in
    ``out_dir``. Returns the page count (``None`` for a document with none, as the worker has it).

    ``beat`` is called while this waits - for the connection, for the answer - so a job that is
    legitimately busy for fifteen minutes is not mistaken for a stuck worker. Raises ``ParserError``
    for every failure of the parser or its answer; an error of the local disk or the upload itself
    is the ``OSError`` it is.
    """
    if kind not in p.KINDS:
        raise ParserError(ParserCode.PARSER_UNSUPPORTED)
    if path.stat().st_size > settings.parser_max_request_bytes:
        raise ParserError(ParserCode.PARSER_TOO_LARGE)
    data = path.read_bytes()
    header: p.Header = {"op": "convert", "kind": kind}
    if p.STEM.fullmatch(path.stem):
        header["stem"] = path.stem  # Docling writes the file's name into its output
    wait = default_reply_timeout_s(settings) if reply_timeout_s is None else reply_timeout_s
    with _connect(settings, beat, clock, sleep) as conn:
        answer, payload = _exchange(conn, header, data, settings, wait, beat)
    if not answer.ok:
        raise ParserError(_FROM_PARSER[answer.code or p.ErrorCode.INTERNAL])
    _checked_document(payload, answer.pages)
    docling_service.save_document_bytes(payload, out_dir)
    return answer.pages or None

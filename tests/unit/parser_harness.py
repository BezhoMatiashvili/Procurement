"""Shared by the parser tests: a fake converter, and the service running in a thread of the test
process (see ``test_parser_server.py``)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

from procurement_agent.parser_service import protocol as p
from procurement_agent.parser_service import server as srv
from procurement_agent.security import egress

MB = 1024 * 1024


def good_converter(path: Path, kind: str) -> srv.Converted:
    return srv.Converted(pages=2, status="success", document=b'{"schema_name": "DoclingDocument"}')


def limits(**overrides: Any) -> srv.Limits:
    base: dict[str, Any] = {
        "max_request_bytes": 1 * MB,
        "max_response_bytes": 1 * MB,
        "max_pages": 300,
        "deadline_s": None,
        "cpu_threads": 4,
        "read_timeout_s": 5.0,
        "write_timeout_s": 5.0,
    }
    base.update(overrides)
    return srv.Limits(**base)


class RecordingGuard:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, int]] = []
        self.fail = fail

    def arm(self, seconds: int, threads: int) -> None:
        if self.fail:
            raise RuntimeError("SIGALRM has a handler")
        self.events.append(("arm", seconds))

    def disarm(self) -> None:
        self.events.append(("disarm", 0))


class Harness:
    def __init__(
        self,
        directory: Path,
        convert_fn: srv.Converter = good_converter,
        lim: srv.Limits | None = None,
        guard: srv.Guard | None = None,
    ) -> None:
        self.path = directory / "p.sock"
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run, args=(convert_fn, lim or limits(), guard), daemon=True
        )

    def _run(self, convert_fn: srv.Converter, lim: srv.Limits, guard: srv.Guard | None) -> None:
        try:
            srv.serve(
                convert_fn=convert_fn,
                socket_path=self.path,
                limits=lim,
                guard=guard,
                stop=self.stop,
                ready=self.ready,
            )
        except BaseException as e:
            self.error.append(e)
            self.ready.set()

    def __enter__(self) -> Harness:
        guard = egress.installed()
        assert guard is not None
        guard.allow_unix(self.path)
        self.thread.start()
        assert self.ready.wait(10), "the server did not start"
        assert not self.error, self.error
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(10)
        assert not self.thread.is_alive()

    def connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(str(self.path))
        return s

    def call(
        self, header: dict[str, Any], payload: bytes = b"", *, cap: int = 4 * MB
    ) -> tuple[p.Header, bytes]:
        with self.connect() as s:
            p.send_frame(s, header, payload)
            return p.read_frame(
                s, limit=lambda h: cap, max_payload=cap, deadline=time.monotonic() + 10
            )

    def convert(self, payload: bytes = b"%PDF-1.4 x", kind: str = "pdf", **extra: Any) -> Any:
        return self.call({"op": "convert", "kind": kind, **extra}, payload)


def docling_json(pages: int = 2, extra: str = "") -> bytes:
    """A document that looks like what ``save_document`` writes, with ``pages`` pages."""
    body = ",".join(f'"{n}": {{"page_no": {n}}}' for n in range(1, pages + 1))
    return f'{{"schema_name": "DoclingDocument", "name": "x{extra}", "pages": {{{body}}}}}'.encode()


def readable_docling_json(pages: int = 2, extra: str = "") -> bytes:
    """A real Docling document with ``pages`` pages and some body text on the first, which is what
    the worker accepts: a parse that holds no text is refused (``NO_TEXT``)."""
    from docling_core.types.doc import BoundingBox, DocItemLabel, Size
    from docling_core.types.doc.document import DoclingDocument, ProvenanceItem

    doc = DoclingDocument(name=f"x{extra}")
    for n in range(1, pages + 1):
        doc.add_page(page_no=n, size=Size(width=100, height=100))
    # a Word file has no pages, and its text no place on one
    prov = (
        ProvenanceItem(page_no=1, bbox=BoundingBox(l=0, t=0, r=10, b=10), charspan=(0, 14))
        if pages
        else None
    )
    doc.add_text(label=DocItemLabel.TEXT, text=f"readable words {extra}".strip(), prov=prov)
    return doc.model_dump_json().encode()


def converter_of(
    pages: int = 2, status: str = "success", extra: str = "", *, readable: bool = False
) -> srv.Converter:
    make = readable_docling_json if readable else docling_json

    def convert(path: Path, kind: str) -> srv.Converted:
        return srv.Converted(pages=pages, status=status, document=make(pages, extra))

    return convert


class LogRecorder:
    """Stands in for a module's ``log``: structlog caches a configured logger on first use, so a
    test that captures it only works until some other test configures logging first."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def record(event: str, **fields: Any) -> None:
            self.events.append({"level": level, "event": event, **fields})

        return record

    def text(self) -> str:
        return str(self.events)

"""The web process's side of the parser socket: one request, an untrusted answer, no second try.

The parser is the real service (``parser_service.server``) in a thread, with a fake converter, for
the cases about conversions; and a few lines of raw socket code for the cases about a parser that
misbehaves - which the real one never does, and the client has to survive anyway, because a parser
in a compromised container is exactly what the sandbox is for.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from procurement_agent.config import Settings
from procurement_agent.ingestion import docling_service, parser_client
from procurement_agent.ingestion.parser_client import ParserCode, ParserError
from procurement_agent.parser_service import protocol as p
from procurement_agent.parser_service import server as srv
from procurement_agent.security import egress
from procurement_agent.security.safe_errors import safe_exc
from tests.unit.parser_harness import (
    MB,
    Harness,
    converter_of,
    docling_json,
    limits,
)

CANARY = "CONFIDENTIAL-CLIENT-TEXT-ბარამიძე"
DOC_ID = "0123456789abcdef0123456789abcdef"


def settings_for(path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "parser_mode": "socket",
        "parser_socket": path,
        "max_file_mb": 1,
        "parser_connect_wait_s": 0.0,
        "docling_timeout_s": 10,
        "parser_kill_grace_s": 1,
    }
    base.update(overrides)
    return Settings(**base)


def upload(directory: Path, data: bytes = b"%PDF-1.4 body", kind: str = "pdf") -> Path:
    path = directory / f"{DOC_ID}.{kind}"
    path.write_bytes(data)
    return path


def run(
    sock_dir: Path,
    harness: Harness,
    *,
    kind: str = "pdf",
    data: bytes = b"%PDF-1.4 body",
    **kw: Any,
) -> tuple[int | None, Path]:
    out = sock_dir / "results" / DOC_ID
    pages = parser_client.convert_through_parser(
        upload(sock_dir, data, kind), kind, out, settings_for(harness.path), **kw
    )
    return pages, out


class RawServer:
    """A parser that is not one: it accepts a connection, reads the request, and does ``act``."""

    def __init__(self, directory: Path, act: Callable[[socket.socket], None]) -> None:
        self.path = directory / "raw.sock"
        self.connections = 0
        self.requests: list[bytes] = []
        self._act = act
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            self.connections += 1
            with conn:
                try:
                    header, payload = p.read_frame(
                        conn,
                        limit=lambda h: 10 * MB,
                        max_payload=10 * MB,
                        deadline=time.monotonic() + 5,
                    )
                    self.requests.append(json.dumps(header).encode() + payload)
                    self._act(conn)
                except (OSError, p.ProtocolError):
                    pass

    def __enter__(self) -> RawServer:
        egress.installed().allow_unix(self.path)  # type: ignore[union-attr]
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(5)
        self._listener.close()


def reply(header: dict[str, Any], payload: bytes = b"") -> Callable[[socket.socket], None]:
    return lambda conn: p.send_frame(conn, header, payload)


def convert_through(raw: RawServer, sock_dir: Path, **overrides: Any) -> Path:
    out = sock_dir / "results" / DOC_ID
    parser_client.convert_through_parser(
        upload(sock_dir), "pdf", out, settings_for(raw.path, **overrides), reply_timeout_s=3.0
    )
    return out


def assert_nothing_stored(out: Path) -> None:
    assert not out.exists() or list(out.iterdir()) == [], "no docling.json, no temporary file"


# --- the conversion --------------------------------------------------------------------------


def test_the_document_the_parser_returns_is_stored_byte_for_byte(sock_dir: Path) -> None:
    expected = docling_json(3, extra="ბარამიძე")
    with Harness(sock_dir, converter_of(3, extra="ბარამიძე")) as h:
        pages, out = run(sock_dir, h)
    assert pages == 3
    assert (out / "docling.json").read_bytes() == expected
    assert [f.name for f in out.iterdir()] == ["docling.json"]
    assert (out / "docling.json").stat().st_mode & 0o777 == 0o600


def test_the_request_names_the_kind_the_stem_and_carries_the_file(sock_dir: Path) -> None:
    seen: dict[str, Any] = {}

    def converter(path: Path, kind: str) -> srv.Converted:
        seen.update(name=path.name, kind=kind, data=path.read_bytes())
        return srv.Converted(1, "success", docling_json(1))

    with Harness(sock_dir, converter) as h:
        run(sock_dir, h, kind="docx", data=b"PK\x03\x04 docx bytes")
    assert seen == {"name": f"{DOC_ID}.docx", "kind": "docx", "data": b"PK\x03\x04 docx bytes"}


def test_a_document_with_no_pages_has_no_page_count(sock_dir: Path) -> None:
    with Harness(sock_dir, converter_of(0)) as h:
        pages, out = run(sock_dir, h, kind="docx")
    assert pages is None
    assert (out / "docling.json").exists()


def test_a_large_document_goes_there_and_back(sock_dir: Path) -> None:
    big = b"%PDF-1.4 " + bytes(range(256)) * 3000
    echoed: list[int] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        echoed.append(len(path.read_bytes()))
        return srv.Converted(1, "success", docling_json(1, extra="y" * 900_000))

    with Harness(sock_dir, converter) as h:
        pages, out = run(sock_dir, h, data=big)
    assert echoed == [len(big)] and pages == 1
    assert len((out / "docling.json").read_bytes()) > 900_000


def test_the_stored_file_replaces_an_earlier_parse(sock_dir: Path) -> None:
    out = sock_dir / "results" / DOC_ID
    out.mkdir(parents=True)
    (out / "docling.json").write_bytes(b"old")
    with Harness(sock_dir, converter_of(1)) as h:
        run(sock_dir, h)
    assert (out / "docling.json").read_bytes() == docling_json(1)


def test_a_sign_of_life_is_given_while_the_parser_works(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(p, "TICK_S", 0.1)
    release = threading.Event()

    def converter(path: Path, kind: str) -> srv.Converted:
        release.wait(0.8)
        return srv.Converted(1, "success", docling_json(1))

    beats: list[int] = []
    with Harness(sock_dir, converter) as h:
        run(sock_dir, h, beat=lambda: beats.append(1))
    assert len(beats) >= 3


# --- the parser says no ----------------------------------------------------------------------


@pytest.mark.parametrize("code", list(p.ErrorCode))
def test_each_error_code_of_the_parser_is_a_fixed_client_code(
    sock_dir: Path, code: p.ErrorCode
) -> None:
    with (
        RawServer(sock_dir, reply(p.error_response_header(code))) as raw,
        pytest.raises(ParserError) as err,
    ):
        convert_through(raw, sock_dir)
    assert err.value.code == ParserCode(f"PARSER_{code.value}")
    assert str(err.value).startswith(f"PARSER_{code.value}: ")


def test_the_real_service_refusing_a_conversion_fails_the_call_with_a_code(
    sock_dir: Path,
) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        raise RuntimeError(CANARY)

    with Harness(sock_dir, converter) as h, pytest.raises(ParserError) as err:
        run(sock_dir, h)
    assert err.value.code is ParserCode.PARSER_CONVERT_FAILED
    assert CANARY not in str(err.value) and CANARY not in safe_exc(err.value)
    assert_nothing_stored(sock_dir / "results" / DOC_ID)


def test_a_request_the_parser_refuses_as_too_large_is_reported_even_though_the_send_broke(
    sock_dir: Path,
) -> None:
    with (
        Harness(sock_dir, lim=limits(max_request_bytes=1000)) as h,
        pytest.raises(ParserError) as err,
    ):
        parser_client.convert_through_parser(
            upload(sock_dir, b"%PDF" + b"x" * 400_000),
            "pdf",
            sock_dir / "r",
            settings_for(h.path),
        )
    assert err.value.code is ParserCode.PARSER_TOO_LARGE


def test_a_file_over_the_request_cap_is_not_sent_at_all(sock_dir: Path) -> None:
    path = upload(sock_dir, b"x" * (2 * MB + 1))  # 1 MB limit + 1 MB of framing
    settings = settings_for(sock_dir / "never.sock")
    with pytest.raises(ParserError) as err:
        parser_client.convert_through_parser(path, "pdf", sock_dir / "r", settings)
    assert err.value.code is ParserCode.PARSER_TOO_LARGE


def test_a_kind_the_parser_does_not_convert_is_not_sent_at_all(sock_dir: Path) -> None:
    with pytest.raises(ParserError) as err:
        parser_client.convert_through_parser(
            upload(sock_dir, kind="xlsx"), "xlsx", sock_dir / "r", settings_for(sock_dir / "n.sock")
        )
    assert err.value.code is ParserCode.PARSER_UNSUPPORTED


# --- the answer is untrusted -----------------------------------------------------------------


def ok_header(pages: int = 1, status: str = "success") -> dict[str, Any]:
    return p.convert_response_header(pages=pages, status=status)


def test_a_result_over_the_response_cap_is_refused_on_its_prefix(sock_dir: Path) -> None:
    def lie(conn: socket.socket) -> None:
        raw = json.dumps(ok_header()).encode()
        conn.sendall(p.PREFIX.pack(p.MAGIC, p.VERSION, len(raw), 10 * 1024**3) + raw)
        time.sleep(0.5)

    with RawServer(sock_dir, lie) as raw, pytest.raises(ParserError) as err:
        convert_through(raw, sock_dir, parser_max_result_mb=1)
    assert err.value.code is ParserCode.PARSER_RESULT_TOO_LARGE
    assert_nothing_stored(sock_dir / "results" / DOC_ID)


def test_a_result_just_over_the_cap_is_refused_too(sock_dir: Path) -> None:
    body = docling_json(1, extra="z" * MB)
    with RawServer(sock_dir, reply(ok_header(), body)) as raw, pytest.raises(ParserError) as err:
        convert_through(raw, sock_dir, parser_max_result_mb=1)
    assert err.value.code is ParserCode.PARSER_RESULT_TOO_LARGE


def test_a_result_exactly_at_the_cap_is_accepted(sock_dir: Path) -> None:
    padding = MB - len(docling_json(1, extra=""))
    body = docling_json(1, extra="z" * padding)
    assert len(body) == MB
    with RawServer(sock_dir, reply(ok_header(), body)) as raw:
        out = convert_through(raw, sock_dir, parser_max_result_mb=1)
    assert (out / "docling.json").read_bytes() == body


GOOD = docling_json(1)


@pytest.mark.parametrize(
    "act",
    [
        pytest.param(lambda c: c.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 40), id="not a frame"),
        pytest.param(
            lambda c: c.sendall(p.PREFIX.pack(b"EVIL", p.VERSION, 2, 0) + b"{}"), id="bad magic"
        ),
        pytest.param(
            lambda c: c.sendall(p.PREFIX.pack(p.MAGIC, 9, 2, 0) + b"{}"), id="another version"
        ),
        pytest.param(reply({"ok": True, "op": "convert"}, GOOD), id="no pages or status"),
        pytest.param(reply(ok_header(1, "failure"), GOOD), id="a status outside the set"),
        pytest.param(reply(ok_header(1, CANARY), GOOD), id="a status that is a document"),
        pytest.param(reply({**ok_header(), "extra": "x"}, GOOD), id="an extra key"),
        pytest.param(reply(ok_header(999), GOOD), id="more pages than the limit"),
        pytest.param(reply({"ok": False, "code": CANARY}), id="an unknown code"),
        pytest.param(
            reply({"ok": False, "code": "CONVERT_FAILED", "message": CANARY}), id="free text"
        ),
        pytest.param(
            reply({"ok": False, "code": "CONVERT_FAILED"}, b"payload"), id="error payload"
        ),
        pytest.param(reply({"ok": True, "op": "ping"}), id="the wrong answer"),
        pytest.param(reply(ok_header(), b"not json at all"), id="a payload that is not JSON"),
        pytest.param(reply(ok_header(), b'["DoclingDocument"]'), id="JSON that is not an object"),
        pytest.param(
            reply(ok_header(), b'{"schema_name": "Other", "pages": {"1": {}}}'),
            id="not a document",
        ),
        pytest.param(
            reply(ok_header(), b'{"schema_name": "DoclingDocument", "pages": []}'), id="pages list"
        ),
        pytest.param(reply(ok_header(2), GOOD), id="pages disagree with the header"),
        pytest.param(reply(ok_header(), b"[" * 100_000), id="nesting deep enough to overflow"),
        pytest.param(reply(ok_header(), b"\xff\xfe\x00"), id="not text"),
    ],
)
def test_an_answer_that_breaks_the_format_is_a_protocol_failure_and_stores_nothing(
    sock_dir: Path, act: Callable[[socket.socket], None]
) -> None:
    with RawServer(sock_dir, act) as raw, pytest.raises(ParserError) as err:
        convert_through(raw, sock_dir)
    assert err.value.code is ParserCode.PARSER_PROTOCOL
    assert CANARY not in str(err.value) and CANARY not in safe_exc(err.value)
    assert_nothing_stored(sock_dir / "results" / DOC_ID)


def test_nothing_of_a_hostile_answer_is_in_the_error_or_its_causes(sock_dir: Path) -> None:
    with (
        RawServer(sock_dir, reply({"ok": False, "code": CANARY, "message": CANARY})) as raw,
        pytest.raises(ParserError) as err,
    ):
        convert_through(raw, sock_dir)
    chain: list[BaseException] = []
    cause: BaseException | None = err.value
    while cause is not None:
        chain.append(cause)
        cause = cause.__cause__ or cause.__context__
    assert len(chain) >= 2
    assert all(CANARY not in repr(e) + str(e) for e in chain)


# --- the parser dies, hangs, is not there ----------------------------------------------------


def test_a_connection_closed_after_the_request_is_a_crash_and_is_not_tried_again(
    sock_dir: Path,
) -> None:
    with RawServer(sock_dir, lambda conn: None) as raw:
        started = time.monotonic()
        with pytest.raises(ParserError) as err:
            convert_through(raw, sock_dir, parser_connect_wait_s=60.0)
        elapsed = time.monotonic() - started
    assert err.value.code is ParserCode.PARSER_CRASHED
    assert raw.connections == 1, "a document that kills the parser is sent once"
    assert elapsed < 3, "and the failure is immediate: it does not wait for the parser to return"


def test_a_frame_cut_off_in_the_middle_is_a_crash(sock_dir: Path) -> None:
    def cut(conn: socket.socket) -> None:
        wire = p.encode_frame(ok_header(), GOOD)
        conn.sendall(wire[:-5])

    with RawServer(sock_dir, cut) as raw, pytest.raises(ParserError) as err:
        convert_through(raw, sock_dir)
    assert err.value.code is ParserCode.PARSER_CRASHED


def test_a_parser_that_does_not_answer_in_time_is_a_timeout(sock_dir: Path) -> None:
    def silent(conn: socket.socket) -> None:
        time.sleep(2.0)

    with RawServer(sock_dir, silent) as raw:
        started = time.monotonic()
        with pytest.raises(ParserError) as err:
            parser_client.convert_through_parser(
                upload(sock_dir),
                "pdf",
                sock_dir / "r",
                settings_for(raw.path),
                reply_timeout_s=0.5,
            )
        assert 0.4 <= time.monotonic() - started < 1.8
    assert err.value.code is ParserCode.PARSER_TIMEOUT


def test_the_default_wait_is_past_the_kill_so_a_kill_is_a_crash_and_not_a_timeout() -> None:
    s = Settings(docling_timeout_s=900, parser_kill_grace_s=30)
    assert parser_client.default_reply_timeout_s(s) == p.reply_deadline_s(900, 30) == 940
    assert parser_client.default_reply_timeout_s(s) > p.kill_deadline_s(900, 30) > 900


def test_no_parser_listening_is_unavailable_after_the_connect_wait(sock_dir: Path) -> None:
    path = sock_dir / "absent.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    started = time.monotonic()
    with pytest.raises(ParserError) as err:
        parser_client.convert_through_parser(
            upload(sock_dir), "pdf", sock_dir / "r", settings_for(path, parser_connect_wait_s=0.6)
        )
    assert err.value.code is ParserCode.PARSER_UNAVAILABLE
    assert 0.5 <= time.monotonic() - started < 3


def test_a_stale_socket_file_nobody_listens_on_is_unavailable_too(sock_dir: Path) -> None:
    path = sock_dir / "stale.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    with pytest.raises(ParserError) as err:
        parser_client.convert_through_parser(
            upload(sock_dir), "pdf", sock_dir / "r", settings_for(path, parser_connect_wait_s=0.3)
        )
    assert err.value.code is ParserCode.PARSER_UNAVAILABLE


def test_the_connect_is_retried_while_the_parser_starts(sock_dir: Path) -> None:
    harness = Harness(sock_dir, converter_of(2))
    starter = threading.Timer(1.0, lambda: harness.__enter__())
    egress.installed().allow_unix(harness.path)  # type: ignore[union-attr]
    starter.start()
    beats: list[int] = []
    try:
        pages = parser_client.convert_through_parser(
            upload(sock_dir),
            "pdf",
            sock_dir / "r",
            settings_for(harness.path, parser_connect_wait_s=20.0),
            beat=lambda: beats.append(1),
        )
        assert pages == 2
        assert beats, "the worker is shown to be alive while it waits for the parser"
    finally:
        starter.cancel()
        starter.join()
        if harness.thread.is_alive():
            harness.__exit__()


def test_a_connection_the_guard_refuses_is_not_retried(sock_dir: Path) -> None:
    path = sock_dir / "not-allowed.sock"
    srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv_sock.bind(str(path))
    srv_sock.listen(1)
    try:
        started = time.monotonic()
        with pytest.raises(ParserError) as err:
            parser_client.convert_through_parser(
                upload(sock_dir),
                "pdf",
                sock_dir / "r",
                settings_for(path, parser_connect_wait_s=30.0),
            )
        assert time.monotonic() - started < 2
    finally:
        srv_sock.close()
    assert err.value.code is ParserCode.PARSER_UNAVAILABLE
    assert isinstance(err.value.__cause__, egress.EgressBlockedError)


def test_the_upload_that_is_missing_is_the_callers_error_not_the_parsers(sock_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parser_client.convert_through_parser(
            sock_dir / f"{DOC_ID}.pdf", "pdf", sock_dir / "r", settings_for(sock_dir / "x.sock")
        )


# --- the messages ----------------------------------------------------------------------------


def test_every_client_code_has_a_fixed_message_and_the_set_is_this_one() -> None:
    assert {c.value for c in ParserCode} == {
        "PARSER_UNAVAILABLE",
        "PARSER_TIMEOUT",
        "PARSER_CRASHED",
        "PARSER_PROTOCOL",
        "PARSER_CONVERT_FAILED",
        "PARSER_TOO_MANY_PAGES",
        "PARSER_UNSUPPORTED",
        "PARSER_BAD_REQUEST",
        "PARSER_TOO_LARGE",
        "PARSER_RESULT_TOO_LARGE",
        "PARSER_INTERNAL",
    }
    for code in ParserCode:
        text = str(ParserError(code))
        assert text.startswith(f"{code.value}: ") and len(text) < 200


def test_a_parser_error_is_a_content_free_conversion_error() -> None:
    error = ParserError(ParserCode.PARSER_CRASHED)
    assert isinstance(error, docling_service.ConversionError)
    assert safe_exc(error).startswith("ParserError: PARSER_CRASHED: ")


def test_the_page_limit_is_the_settings_limit(sock_dir: Path) -> None:
    with RawServer(sock_dir, reply(ok_header(5), docling_json(5))) as raw:
        with pytest.raises(ParserError) as err:
            convert_through(raw, sock_dir, max_pages=4)
        assert err.value.code is ParserCode.PARSER_PROTOCOL
        out = convert_through(raw, sock_dir, max_pages=5)
    assert (out / "docling.json").exists()


def test_a_file_whose_name_is_not_a_document_id_is_sent_without_a_stem(sock_dir: Path) -> None:
    names: list[str] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        names.append(path.name)
        return srv.Converted(1, "success", docling_json(1))

    odd = sock_dir / "my report (1).pdf"
    odd.write_bytes(b"%PDF-1.4")
    with Harness(sock_dir, converter) as h:
        parser_client.convert_through_parser(odd, "pdf", sock_dir / "r", settings_for(h.path))
    assert names == ["document.pdf"]


def test_a_parser_that_hangs_up_while_the_file_is_still_being_sent_is_still_heard(
    sock_dir: Path,
) -> None:
    path = sock_dir / "rude.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def hang_up() -> None:
        conn, _ = listener.accept()
        p.send_frame(conn, p.error_response_header(p.ErrorCode.TOO_LARGE))
        conn.close()  # without reading a byte of the request

    t = threading.Thread(target=hang_up, daemon=True)
    t.start()
    try:
        with pytest.raises(ParserError) as err:
            parser_client.convert_through_parser(
                upload(sock_dir, b"%PDF" + b"x" * 1_900_000),
                "pdf",
                sock_dir / "r",
                settings_for(path, max_file_mb=2),
            )
    finally:
        t.join(10)
        listener.close()
    # the answer was written before the hang-up; if the kernel dropped it with the reset, the
    # failure is still a failure of the parser, never an error of this process
    assert err.value.code in (ParserCode.PARSER_TOO_LARGE, ParserCode.PARSER_CRASHED)


def test_a_parser_that_does_not_read_the_file_is_a_timeout_while_sending(sock_dir: Path) -> None:
    path = sock_dir / "deaf.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    held: list[socket.socket] = []
    t = threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True)
    t.start()
    try:
        started = time.monotonic()
        with pytest.raises(ParserError) as err:
            parser_client.convert_through_parser(
                upload(sock_dir, b"%PDF" + b"x" * 1_900_000),
                "pdf",
                sock_dir / "r",
                settings_for(path, max_file_mb=2),
                reply_timeout_s=0.5,
            )
        assert time.monotonic() - started < 3
    finally:
        t.join(5)
        for c in held:
            c.close()
        listener.close()
    assert err.value.code is ParserCode.PARSER_TIMEOUT

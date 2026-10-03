"""The parser's wire format: one frame = fixed prefix + small JSON header + raw payload.

No socket is involved: the reader takes anything with ``recv``, so every case is a byte string.
What is asserted is that the receiving side decides from the prefix and the header alone - before it
reads, let alone keeps, a payload - and that nothing a peer sends reaches an error message.
"""

from __future__ import annotations

import json
import random
import struct

import pytest

from procurement_agent.parser_service import protocol as p

CANARY = "CONFIDENTIAL-CLIENT-TEXT-ბარამიძე"


class Pipe:
    """A byte string that is read like a socket: in pieces, then end of file."""

    def __init__(self, data: bytes, piece: int = 7, stall: int = 0) -> None:
        self.data = data
        self.pos = 0
        self.piece = piece
        self.stall = stall  # this many reads raise TimeoutError first, as a quiet peer does
        self.timeouts: list[float | None] = []
        self.sent = bytearray()

    def recv(self, bufsize: int, /) -> bytes:
        if self.stall > 0:
            self.stall -= 1
            raise TimeoutError
        chunk = self.data[self.pos : self.pos + min(bufsize, self.piece)]
        self.pos += len(chunk)
        return chunk

    def sendall(self, data: bytes, /) -> None:
        self.sent += data

    def settimeout(self, value: float | None, /) -> None:
        self.timeouts.append(value)


def frame(
    header: object,
    payload: bytes = b"",
    *,
    hlen: int | None = None,
    plen: int | None = None,
    magic: bytes = p.MAGIC,
    version: int = p.VERSION,
) -> bytes:
    raw = header if isinstance(header, bytes) else json.dumps(header).encode()
    prefix = struct.pack(
        ">4sBIQ",
        magic,
        version,
        len(raw) if hlen is None else hlen,
        len(payload) if plen is None else plen,
    )
    return prefix + raw + payload


def read(data: bytes, *, cap: int = 1000, deadline: float = 1e9, **kw: object):
    return p.read_frame(
        Pipe(data, **kw),  # type: ignore[arg-type]
        limit=lambda header: cap,
        max_payload=cap,
        deadline=deadline,
        clock=lambda: 0.0,
    )


# --- the happy path --------------------------------------------------------------------------


def test_a_frame_round_trips_in_any_piece_size() -> None:
    wire = p.encode_frame({"op": "convert", "kind": "pdf"}, b"%PDF-" + bytes(range(256)) * 9)
    for piece in (1, 2, 5, 64, 100_000):
        header, payload = read(wire, cap=5000, piece=piece)
        assert header == {"op": "convert", "kind": "pdf"}
        assert payload == b"%PDF-" + bytes(range(256)) * 9


def test_the_prefix_is_big_endian_with_magic_and_version() -> None:
    wire = p.encode_frame({"op": "ping"})
    assert wire[:4] == b"PAPS"
    assert wire[4] == p.VERSION
    header_len, payload_len = struct.unpack(">IQ", wire[5:17])
    assert (header_len, payload_len) == (len(b'{"op": "ping"}'), 0)
    assert p.PREFIX.size == 17


def test_send_frame_writes_the_header_and_the_payload() -> None:
    pipe = Pipe(b"")
    p.send_frame(pipe, {"op": "ping"})  # type: ignore[arg-type]
    assert bytes(pipe.sent) == p.encode_frame({"op": "ping"})
    big = Pipe(b"")
    p.send_frame(big, {"op": "convert", "kind": "docx"}, b"x" * 50_000)  # type: ignore[arg-type]
    assert bytes(big.sent) == p.encode_frame({"op": "convert", "kind": "docx"}, b"x" * 50_000)


# --- the caps are enforced before anything is read --------------------------------------------


def test_a_payload_longer_than_the_cap_is_refused_on_the_prefix_alone() -> None:
    pipe = Pipe(frame({"op": "ping"}, plen=2**40))  # claims a terabyte, sends nothing
    with pytest.raises(p.FrameTooLargeError):
        p.read_frame(pipe, limit=lambda h: 10, max_payload=10, deadline=1e9, clock=lambda: 0.0)
    assert pipe.pos == p.PREFIX.size, "nothing past the prefix was read"


def test_the_cap_of_an_operation_is_applied_before_its_payload_is_read() -> None:
    wire = frame({"op": "ping"}, b"x" * 500)
    pipe = Pipe(wire)
    caps = {"ping": 0}
    with pytest.raises(p.FrameTooLargeError):
        p.read_frame(
            pipe,
            limit=lambda h: caps[str(h["op"])],
            max_payload=1000,
            deadline=1e9,
            clock=lambda: 0.0,
        )
    assert pipe.pos == p.PREFIX.size + len(json.dumps({"op": "ping"}))


def test_a_header_longer_than_four_kib_is_refused_without_reading_it() -> None:
    pipe = Pipe(frame(b"x" * 10, hlen=p.MAX_HEADER_BYTES + 1))
    with pytest.raises(p.ProtocolError) as err:
        p.read_frame(pipe, limit=lambda h: 0, max_payload=0, deadline=1e9, clock=lambda: 0.0)
    assert err.value.code is p.ErrorCode.BAD_REQUEST
    assert pipe.pos == p.PREFIX.size
    assert p.MAX_HEADER_BYTES == 4096


def test_an_empty_header_is_refused() -> None:
    with pytest.raises(p.ProtocolError):
        read(frame(b""))


def test_a_header_of_exactly_four_kib_is_read() -> None:
    filler = "a" * (p.MAX_HEADER_BYTES - len('{"op":"ping","x":""}'))
    raw = ('{"op":"ping","x":"' + filler + '"}').encode()
    assert len(raw) == p.MAX_HEADER_BYTES
    header, _ = read(frame(raw))
    assert header["op"] == "ping"


# --- malformed and truncated frames -----------------------------------------------------------


def test_the_wrong_magic_is_refused() -> None:
    with pytest.raises(p.ProtocolError) as err:
        read(frame({"op": "ping"}, magic=b"GET "))
    assert err.value.code is p.ErrorCode.BAD_REQUEST


def test_another_version_is_unsupported_not_malformed() -> None:
    with pytest.raises(p.ProtocolError) as err:
        read(frame({"op": "ping"}, version=p.VERSION + 1))
    assert err.value.code is p.ErrorCode.UNSUPPORTED


def test_a_peer_that_closes_before_sending_anything_is_a_closed_connection() -> None:
    with pytest.raises(p.ConnectionClosedError):
        read(b"")


@pytest.mark.parametrize("cut", [1, 5, 16, 17, 20, 30])
def test_a_frame_cut_short_anywhere_is_truncated(cut: int) -> None:
    wire = frame({"op": "convert", "kind": "pdf"}, b"payload-bytes")
    assert cut < len(wire)
    with pytest.raises(p.TruncatedError):
        read(wire[:cut])


def test_a_payload_cut_short_is_truncated() -> None:
    wire = frame({"op": "convert", "kind": "pdf"}, b"p" * 100)
    with pytest.raises(p.TruncatedError):
        read(wire[:-1])


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b'"op"',
        b"null",
        b'{"op": "ping", "op": "convert"}',  # a duplicate key
        b'{"op": NaN}',
        b'{"op": Infinity}',
        b'{"n": 1.5}',
        b'{"n": 1e3}',
        b'{"a": [1]}',
        b'{"a": {"b": 1}}',
        b'{"a": null}',
        b'{"a": 4294967296}',
        b'{"a": -4294967296}',
        b'{"\xff": 1}',
        b'{"a": "\xff"}',
        b"{" * 3000,
        b'{"a":' * 700 + b"1" + b"}" * 700,
        b'{"' + b"k" * 40 + b'": 1}',
        b"{" + b",".join(b'"k%d": 1' % i for i in range(20)) + b"}",
    ],
)
def test_a_header_that_is_not_a_flat_object_of_small_scalars_is_refused(raw: bytes) -> None:
    with pytest.raises(p.ProtocolError) as err:
        read(frame(raw))
    assert err.value.code is p.ErrorCode.BAD_REQUEST


def test_a_boolean_is_not_a_number_and_a_number_is_not_a_boolean() -> None:
    header, _ = read(frame({"ok": True, "pages": 3}))
    assert header["ok"] is True and header["pages"] == 3
    assert type(header["ok"]) is bool and type(header["pages"]) is int


def test_a_quiet_peer_costs_ticks_and_then_the_deadline() -> None:
    ticks: list[int] = []
    now = [0.0]

    def clock() -> float:
        return now[0]

    pipe = Pipe(b"", stall=10**9)

    def tick() -> None:
        ticks.append(1)
        now[0] += 3.0

    with pytest.raises(p.FrameTimeoutError):
        p.read_frame(pipe, limit=lambda h: 0, max_payload=0, deadline=10.0, clock=clock, tick=tick)
    assert 3 <= len(ticks) <= 5
    assert all(t is not None and t <= p.TICK_S for t in pipe.timeouts)


def test_a_slow_but_live_peer_is_read_to_the_end() -> None:
    wire = p.encode_frame({"op": "ping"})
    header, _ = read(wire, stall=3, piece=3)
    assert header == {"op": "ping"}


def test_random_garbage_never_escapes_as_anything_but_a_protocol_error() -> None:
    rng = random.Random(20261003)
    kinds = (p.ProtocolError,)
    outcomes = {"refused": 0, "read": 0}
    good = p.encode_frame({"op": "convert", "kind": "pdf"}, b"abc")
    for _ in range(600):
        mode = rng.randrange(4)
        if mode == 0:
            data = rng.randbytes(rng.randrange(0, 80))
        elif mode == 1:
            raw = bytearray(good)
            for _ in range(rng.randrange(1, 4)):
                raw[rng.randrange(len(raw))] = rng.randrange(256)
            data = bytes(raw)
        elif mode == 2:
            data = good[: rng.randrange(len(good))]
        else:
            data = p.PREFIX.pack(
                p.MAGIC, p.VERSION, rng.randrange(0, 6000), rng.randrange(0, 2**63)
            )
            data += rng.randbytes(rng.randrange(0, 60))
        try:
            read(data, cap=64)
        except kinds:
            outcomes["refused"] += 1
        else:
            outcomes["read"] += 1
    assert outcomes["refused"] > 300  # the generator really makes bad frames


def test_error_messages_carry_a_reason_and_nothing_a_peer_sent() -> None:
    hostile = [
        frame({"op": CANARY, "kind": CANARY}),
        frame(('{"' + CANARY + '": 1, "' + CANARY + '": 2}').encode()),
        frame(CANARY.encode(), magic=b"EVIL"),
        frame(b"{" + CANARY.encode()),
    ]
    for data in hostile:
        with pytest.raises(p.ProtocolError) as err:
            header, _ = read(data)
            p.check_request(header)  # a well-formed header with a request nobody makes
        assert CANARY not in str(err.value) and CANARY not in repr(err.value)
        assert str(err.value) in p.REASONS


# --- requests ---------------------------------------------------------------------------------


def test_a_ping_and_a_convert_request_are_understood() -> None:
    assert p.check_request({"op": "ping"}) == p.Request(op="ping")
    assert p.check_request({"op": "convert", "kind": "docx"}) == p.Request(
        op="convert", kind="docx"
    )
    assert p.check_request({"op": "convert", "kind": "pdf", "stem": "a1_B-2"}) == p.Request(
        op="convert", kind="pdf", stem="a1_B-2"
    )


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ({}, p.ErrorCode.BAD_REQUEST),
        ({"op": 3}, p.ErrorCode.BAD_REQUEST),
        ({"op": "ping", "kind": "pdf"}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert"}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": 1}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "pdf", "extra": "x"}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "pdf", "stem": 5}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "pdf", "stem": "../etc"}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "pdf", "stem": "a/b"}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "pdf", "stem": ""}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "pdf", "stem": "x" * 65}, p.ErrorCode.BAD_REQUEST),
        ({"op": "convert", "kind": "xlsx"}, p.ErrorCode.UNSUPPORTED),
        ({"op": "convert", "kind": "PDF"}, p.ErrorCode.UNSUPPORTED),
        ({"op": "shutdown"}, p.ErrorCode.UNSUPPORTED),
        ({"op": "eval"}, p.ErrorCode.UNSUPPORTED),
    ],
)
def test_a_request_outside_the_allow_list_is_refused_with_its_code(
    header: dict[str, object], code: p.ErrorCode
) -> None:
    with pytest.raises(p.ProtocolError) as err:
        p.check_request(header)  # type: ignore[arg-type]
    assert err.value.code is code


def test_the_payload_cap_of_each_operation() -> None:
    assert p.request_payload_limit(p.Request(op="ping"), 9_000) == 0
    assert p.request_payload_limit(p.Request(op="convert", kind="pdf"), 9_000) == 9_000


# --- responses --------------------------------------------------------------------------------


def test_the_responses_the_server_writes_are_the_ones_the_client_accepts() -> None:
    ping = p.check_response({"ok": True, "op": "ping"}, "ping", max_pages=300)
    assert ping.ok
    wire = p.convert_response_header(pages=12, status="partial_success")
    done = p.check_response(wire, "convert", max_pages=300)
    assert (done.ok, done.pages, done.status) == (True, 12, "partial_success")
    for code in p.ErrorCode:
        failed = p.check_response(p.error_response_header(code), "convert", max_pages=300)
        assert (failed.ok, failed.code) == (False, code)


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"ok": 1, "op": "ping"},
        {"ok": True},
        {"ok": True, "op": "convert"},
        {"ok": True, "op": "ping", "pages": 1},
        {"ok": True, "op": "convert", "pages": 3},
        {"ok": True, "op": "convert", "pages": "3", "status": "success"},
        {"ok": True, "op": "convert", "pages": True, "status": "success"},
        {"ok": True, "op": "convert", "pages": -1, "status": "success"},
        {"ok": True, "op": "convert", "pages": 301, "status": "success"},
        {"ok": True, "op": "convert", "pages": 3, "status": "failure"},
        {"ok": True, "op": "convert", "pages": 3, "status": CANARY},
        {"ok": True, "op": "ping", "extra": "x"},
        {"ok": False},
        {"ok": False, "code": "NOT_A_CODE"},
        {"ok": False, "code": CANARY},
        {"ok": False, "code": "CONVERT_FAILED", "message": "free text"},
        {"ok": False, "code": 7},
        {"ok": "yes", "code": "CONVERT_FAILED"},
    ],
)
def test_a_response_outside_the_allow_list_is_a_protocol_error_that_echoes_nothing(
    header: dict[str, object],
) -> None:
    op = "ping" if header.get("op") == "ping" else "convert"
    with pytest.raises(p.ProtocolError) as err:
        p.check_response(header, op, max_pages=300)  # type: ignore[arg-type]
    assert CANARY not in str(err.value)


def test_a_response_for_the_wrong_operation_is_refused() -> None:
    with pytest.raises(p.ProtocolError):
        p.check_response({"ok": True, "op": "ping"}, "convert", max_pages=300)


def test_error_codes_are_a_fixed_enumeration() -> None:
    assert {c.value for c in p.ErrorCode} == {
        "CONVERT_FAILED",
        "TOO_MANY_PAGES",
        "UNSUPPORTED",
        "BAD_REQUEST",
        "TOO_LARGE",
        "RESULT_TOO_LARGE",
        "INTERNAL",
    }


def test_encoding_refuses_a_header_that_decoding_would_refuse() -> None:
    with pytest.raises(ValueError, match="header"):
        p.encode_frame({"op": "ping", "x": "y" * 5000})
    with pytest.raises(ValueError, match="header"):
        p.encode_frame({"op": "ping", "x": [1]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="header"):
        p.encode_frame({"op": "ping", "x": 2**40})


# --- deadlines --------------------------------------------------------------------------------


def test_the_kill_comes_after_docling_gives_up_and_the_reply_wait_after_the_kill() -> None:
    assert p.kill_deadline_s(900, 30) == 930
    assert p.reply_deadline_s(900, 30) == 930 + p.REPLY_SLACK_S

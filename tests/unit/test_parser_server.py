"""The parser service: one request at a time on a Unix socket, with a converter that is injected.

The server runs in a thread of the test process, on a socket in a short temporary directory (a Unix
address holds 107 bytes, which a pytest ``tmp_path`` can exceed) and with a fake converter, so what
is asserted is the service - framing, caps, error codes, cleanup, one request at a time, the kill
switch being armed around the conversion and nowhere else - and not Docling. The kill switch itself
is proved for real, in a subprocess, by ``test_parser_kill.py``.
"""

from __future__ import annotations

import contextlib
import socket
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from procurement_agent.parser_service import protocol as p
from procurement_agent.parser_service import server as srv
from procurement_agent.security import egress
from tests.unit.parser_harness import (
    MB,
    Harness,
    LogRecorder,
    RecordingGuard,
    good_converter,
    limits,
)

CANARY = "CONFIDENTIAL-CLIENT-TEXT-ბარამიძე"


# --- ping and convert ------------------------------------------------------------------------


def test_a_ping_is_answered(sock_dir: Path) -> None:
    with Harness(sock_dir) as h:
        header, payload = h.call({"op": "ping"})
    assert header == {"ok": True, "op": "ping"} and payload == b""


def test_a_conversion_returns_the_converters_bytes_with_pages_and_status(sock_dir: Path) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        return srv.Converted(pages=7, status="partial_success", document=b"\x00any bytes\xff")

    with Harness(sock_dir, converter) as h:
        header, payload = h.convert()
    assert header == {"ok": True, "op": "convert", "pages": 7, "status": "partial_success"}
    assert payload == b"\x00any bytes\xff"


def test_the_document_is_given_to_the_converter_as_a_private_file_and_removed_after(
    sock_dir: Path,
) -> None:
    seen: dict[str, Any] = {}

    def converter(path: Path, kind: str) -> srv.Converted:
        seen["name"] = path.name
        seen["bytes"] = path.read_bytes()
        seen["file_mode"] = stat.S_IMODE(path.stat().st_mode)
        seen["dir_mode"] = stat.S_IMODE(path.parent.stat().st_mode)
        seen["dir"] = path.parent
        seen["kind"] = kind
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        h.convert(b"%PDF-1.7 the bytes", "pdf", stem="0123abcd")
    assert seen["name"] == "0123abcd.pdf"
    assert seen["bytes"] == b"%PDF-1.7 the bytes"
    assert seen["kind"] == "pdf"
    assert seen["file_mode"] & 0o077 == 0 and seen["dir_mode"] & 0o077 == 0
    assert not seen["dir"].exists(), "the private directory is gone once the answer is sent"


def test_without_a_stem_the_file_is_called_document(sock_dir: Path) -> None:
    names: list[str] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        names.append(path.name)
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        h.convert(kind="docx")
    assert names == ["document.docx"]


def test_the_working_directory_is_cleaned_even_when_the_conversion_fails(sock_dir: Path) -> None:
    work = sock_dir / "work"
    work.mkdir()
    dirs: list[Path] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        dirs.append(path.parent)
        raise ValueError(CANARY)

    path = sock_dir / "p.sock"
    stop, ready = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=lambda: srv.serve(
            convert_fn=converter,
            socket_path=path,
            limits=limits(),
            stop=stop,
            ready=ready,
            workdir=work,
        ),
        daemon=True,
    )
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    thread.start()
    assert ready.wait(10)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(path))
            p.send_frame(s, {"op": "convert", "kind": "pdf"}, b"x")
            header, _ = p.read_frame(
                s, limit=lambda h: 0, max_payload=0, deadline=time.monotonic() + 10
            )
        assert header == {"ok": False, "code": "CONVERT_FAILED"}
    finally:
        stop.set()
        thread.join(10)
    assert dirs and not dirs[0].exists() and list(work.iterdir()) == []


# --- errors cross the wire as codes and nothing else -----------------------------------------


def test_a_failing_converter_is_a_code_never_a_message_and_logs_no_content(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        raise RuntimeError(f"could not parse {CANARY}")

    recorder = LogRecorder()
    monkeypatch.setattr(srv, "log", recorder)
    with Harness(sock_dir, converter) as h:
        header, payload = h.convert(CANARY.encode())
    assert header == {"ok": False, "code": "CONVERT_FAILED"} and payload == b""
    assert CANARY not in recorder.text()
    assert any(entry["event"] == "parser_convert_failed" for entry in recorder.events)


def test_the_server_keeps_serving_after_a_failure(sock_dir: Path) -> None:
    calls = [0]

    def converter(path: Path, kind: str) -> srv.Converted:
        calls[0] += 1
        if calls[0] == 1:
            raise MemoryError
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        assert h.convert()[0] == {"ok": False, "code": "CONVERT_FAILED"}
        assert h.convert()[0]["ok"] is True


def test_more_pages_than_the_limit_is_too_many_pages(sock_dir: Path) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        return srv.Converted(pages=301, status="success", document=b"{}")

    with Harness(sock_dir, converter, limits(max_pages=300)) as h:
        assert h.convert()[0] == {"ok": False, "code": "TOO_MANY_PAGES"}


@pytest.mark.parametrize("status", ["failure", "", CANARY])
def test_a_status_outside_the_fixed_set_is_an_internal_error(sock_dir: Path, status: str) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        return srv.Converted(pages=1, status=status, document=b"{}")

    with Harness(sock_dir, converter) as h:
        header, _ = h.convert()
    assert header == {"ok": False, "code": "INTERNAL"}


def test_a_result_over_the_response_cap_is_refused_not_sent(sock_dir: Path) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        return srv.Converted(pages=1, status="success", document=b"x" * 1001)

    with Harness(sock_dir, converter, limits(max_response_bytes=1000)) as h:
        header, payload = h.convert()
    assert header == {"ok": False, "code": "RESULT_TOO_LARGE"} and payload == b""


def test_a_result_of_exactly_the_cap_is_sent(sock_dir: Path) -> None:
    def converter(path: Path, kind: str) -> srv.Converted:
        return srv.Converted(pages=1, status="success", document=b"x" * 1000)

    with Harness(sock_dir, converter, limits(max_response_bytes=1000)) as h:
        header, payload = h.convert()
    assert header["ok"] is True and len(payload) == 1000


def test_a_request_over_the_cap_is_refused_before_it_is_read(sock_dir: Path) -> None:
    called: list[int] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        called.append(1)
        return good_converter(path, kind)

    with Harness(sock_dir, converter, limits(max_request_bytes=1000)) as h:
        with h.connect() as s:
            p.send_frame(s, {"op": "convert", "kind": "pdf"}, b"x" * 3000)
            header, _ = p.read_frame(
                s, limit=lambda x: 0, max_payload=0, deadline=time.monotonic() + 10
            )
        assert header == {"ok": False, "code": "TOO_LARGE"}
        assert h.call({"op": "ping"})[0]["ok"] is True
    assert called == []


def test_a_ping_with_a_payload_is_too_large(sock_dir: Path) -> None:
    with Harness(sock_dir) as h:
        header, _ = h.call({"op": "ping"}, b"abc")
    assert header == {"ok": False, "code": "TOO_LARGE"}


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ({"op": "shutdown"}, "UNSUPPORTED"),
        ({"op": "convert", "kind": "xlsx"}, "UNSUPPORTED"),
        ({"op": "convert", "kind": "pdf", "path": "/etc/passwd"}, "BAD_REQUEST"),
        ({"op": "convert", "kind": "pdf", "stem": "../../etc/x"}, "BAD_REQUEST"),
        ({"kind": "pdf"}, "BAD_REQUEST"),
    ],
)
def test_a_request_outside_the_allow_list_is_refused_with_its_code(
    sock_dir: Path, header: dict[str, Any], code: str
) -> None:
    called: list[int] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        called.append(1)
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        answer, _ = h.call(header, b"x")
    assert answer == {"ok": False, "code": code}
    assert called == []


def test_garbage_and_cut_off_frames_do_not_stop_the_server(sock_dir: Path) -> None:
    with Harness(sock_dir) as h:
        for junk in (
            b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" + b"x" * 40,
            p.encode_frame({"op": "ping"})[:9],
            p.PREFIX.pack(p.MAGIC, p.VERSION + 1, 10, 0) + b"0123456789",
            b"",
        ):
            with h.connect() as s:
                if junk:
                    s.sendall(junk)
                s.shutdown(socket.SHUT_WR)
                with contextlib.suppress(OSError):
                    s.recv(4096)
        assert h.call({"op": "ping"})[0]["ok"] is True


def test_a_silent_client_costs_the_read_timeout_and_no_more(sock_dir: Path) -> None:
    with Harness(sock_dir, lim=limits(read_timeout_s=0.4)) as h:
        quiet = h.connect()
        try:
            started = time.monotonic()
            assert h.call({"op": "ping"})[0]["ok"] is True
            assert time.monotonic() - started < 5
        finally:
            quiet.close()


def test_a_client_that_leaves_during_the_conversion_does_not_stop_the_server(
    sock_dir: Path,
) -> None:
    started, release = threading.Event(), threading.Event()

    def converter(path: Path, kind: str) -> srv.Converted:
        started.set()
        release.wait(10)
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        s = h.connect()
        p.send_frame(s, {"op": "convert", "kind": "pdf"}, b"x")
        assert started.wait(10)
        s.close()
        release.set()
        assert h.call({"op": "ping"})[0]["ok"] is True


# --- one request at a time -------------------------------------------------------------------


def test_requests_are_served_one_at_a_time_in_arrival_order(sock_dir: Path) -> None:
    started, release = threading.Event(), threading.Event()
    order: list[str] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        order.append("start")
        started.set()
        release.wait(10)
        order.append("end")
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        first = h.connect()
        p.send_frame(first, {"op": "convert", "kind": "pdf"}, b"x")
        assert started.wait(10)
        second = h.connect()
        p.send_frame(second, {"op": "ping"})
        second.settimeout(0.5)
        with pytest.raises(TimeoutError):  # not answered while the first conversion runs
            second.recv(1)
        release.set()
        second.settimeout(10)
        header, _ = p.read_frame(
            second, limit=lambda x: 0, max_payload=0, deadline=time.monotonic() + 10
        )
        assert header == {"ok": True, "op": "ping"}
        first.close()
        second.close()
    assert order == ["start", "end"]


# --- the kill switch is armed around the conversion and nowhere else -------------------------


def test_the_kill_switch_is_armed_for_the_conversion_only(sock_dir: Path) -> None:
    guard = RecordingGuard()
    seen: list[list[tuple[str, int]]] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        seen.append(list(guard.events))
        return good_converter(path, kind)

    with Harness(sock_dir, converter, limits(deadline_s=77), guard) as h:
        h.call({"op": "ping"})
        assert guard.events == []
        h.convert()
    assert seen == [[("arm", 77)]]
    assert guard.events == [("arm", 77), ("disarm", 0)]


def test_the_kill_switch_is_disarmed_when_the_conversion_fails(sock_dir: Path) -> None:
    guard = RecordingGuard()

    def converter(path: Path, kind: str) -> srv.Converted:
        raise ValueError

    with Harness(sock_dir, converter, limits(deadline_s=5), guard) as h:
        h.convert()
    assert guard.events == [("arm", 5), ("disarm", 0)]


def test_a_conversion_never_runs_without_its_kill_switch(sock_dir: Path) -> None:
    called: list[int] = []

    def converter(path: Path, kind: str) -> srv.Converted:
        called.append(1)
        return good_converter(path, kind)

    with Harness(sock_dir, converter, limits(deadline_s=5), RecordingGuard(fail=True)) as h:
        header, _ = h.convert()
    assert header == {"ok": False, "code": "INTERNAL"}
    assert called == []


def test_without_a_deadline_nothing_is_armed(sock_dir: Path) -> None:
    guard = RecordingGuard()
    with Harness(sock_dir, lim=limits(deadline_s=None), guard=guard) as h:
        h.convert()
    assert guard.events == []


# --- the real kill switch, with the system calls replaced -----------------------------------


class Syscalls:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, handler: object = None) -> None:
        import resource
        import signal

        self.alarms: list[int] = []
        self.limits: list[tuple[int, int]] = []
        self.handlers: list[tuple[int, object]] = []
        self.current = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        handler = signal.SIG_DFL if handler is None else handler
        monkeypatch.setattr(signal, "alarm", lambda s: self.alarms.append(s) or 0)
        monkeypatch.setattr(signal, "getsignal", lambda n: handler)
        monkeypatch.setattr(signal, "signal", lambda n, h: self.handlers.append((n, h)))
        monkeypatch.setattr(resource, "getrlimit", lambda r: self.current)
        monkeypatch.setattr(resource, "setrlimit", lambda r, lim: self.limits.append(lim))
        usage = type("U", (), {"ru_utime": 40.2, "ru_stime": 1.5})()
        monkeypatch.setattr(resource, "getrusage", lambda who: usage)


def test_arming_sets_the_alarm_and_a_cpu_limit_above_what_was_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Syscalls(monkeypatch)
    switch = srv.KillSwitch()
    switch.arm(930, 12)
    assert calls.alarms == [930]
    soft, _ = calls.limits[-1]
    assert soft == 41 + 1 + 930 * 12  # whole seconds used so far, plus the deadline per thread
    switch.disarm()
    assert calls.alarms == [930, 0]


def test_disarming_puts_the_cpu_limit_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    calls = Syscalls(monkeypatch)
    calls.current = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    switch = srv.KillSwitch()
    switch.arm(10, 1)
    calls.current = calls.limits[-1]
    switch.disarm()
    assert calls.limits[-1] == (resource.RLIM_INFINITY, resource.RLIM_INFINITY)


def test_the_cpu_limit_never_exceeds_the_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Syscalls(monkeypatch)
    calls.current = (500, 600)
    srv.KillSwitch().arm(930, 12)
    assert calls.limits[-1] == (600, 600)


def test_arming_is_refused_when_the_alarm_signal_has_a_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Syscalls(monkeypatch, handler=lambda *a: None)
    with pytest.raises(RuntimeError, match="SIGALRM"):
        srv.KillSwitch().arm(10, 1)
    assert calls.alarms == [] and calls.limits == []


def test_installing_restores_the_default_action_of_both_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    calls = Syscalls(monkeypatch)
    srv.KillSwitch().install()
    assert calls.handlers == [
        (signal.SIGALRM, signal.SIG_DFL),
        (signal.SIGXCPU, signal.SIG_DFL),
    ]


# --- binding ---------------------------------------------------------------------------------


def test_the_socket_is_group_readable_and_writable_and_closed_to_the_world(sock_dir: Path) -> None:
    with Harness(sock_dir) as h:
        assert stat.S_IMODE(h.path.stat().st_mode) == 0o660
        assert stat.S_ISSOCK(h.path.stat().st_mode)
    assert not h.path.exists(), "a stopped server leaves no socket behind"


def test_a_stale_socket_from_a_killed_server_is_replaced(sock_dir: Path) -> None:
    stale = sock_dir / "p.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(stale))
    dead.close()  # the file stays, nobody listens: what a SIGKILL leaves
    assert stale.exists()
    with Harness(sock_dir) as h:
        assert h.call({"op": "ping"})[0]["ok"] is True


def test_a_file_that_is_not_a_socket_is_never_deleted(sock_dir: Path) -> None:
    precious = sock_dir / "p.sock"
    precious.write_text("not a socket")
    with pytest.raises(RuntimeError, match="not a socket"):
        srv.serve(
            convert_fn=good_converter, socket_path=precious, limits=limits(), stop=threading.Event()
        )
    assert precious.read_text() == "not a socket"


def test_a_directory_the_world_can_enter_is_refused(sock_dir: Path) -> None:
    sock_dir.chmod(0o755)
    with pytest.raises(RuntimeError, match="directory"):
        srv.serve(
            convert_fn=good_converter,
            socket_path=sock_dir / "p.sock",
            limits=limits(),
            stop=threading.Event(),
        )
    assert not (sock_dir / "p.sock").exists()


def test_a_missing_directory_and_a_path_too_long_for_a_unix_address_are_refused(
    sock_dir: Path,
) -> None:
    with pytest.raises(RuntimeError, match="directory"):
        srv.serve(
            convert_fn=good_converter,
            socket_path=sock_dir / "nope" / "p.sock",
            limits=limits(),
            stop=threading.Event(),
        )
    long_dir = sock_dir / ("d" * 100)
    long_dir.mkdir()
    with pytest.raises(RuntimeError, match="too long"):
        srv.serve(
            convert_fn=good_converter,
            socket_path=long_dir / "p.sock",
            limits=limits(),
            stop=threading.Event(),
        )


def test_the_umask_in_force_does_not_loosen_the_socket(sock_dir: Path) -> None:
    import os

    previous = os.umask(0)
    try:
        with Harness(sock_dir) as h:
            assert stat.S_IMODE(h.path.stat().st_mode) == 0o660
            assert os.umask(0) == 0, "binding restored the umask it changed"
    finally:
        os.umask(previous)


# --- limits from the settings ----------------------------------------------------------------


def test_the_limits_are_the_settings_limits() -> None:
    from procurement_agent.config import Settings

    s = Settings(
        max_file_mb=7,
        max_pages=11,
        parser_max_result_mb=3,
        docling_timeout_s=100,
        parser_kill_grace_s=20,
        docling_num_threads=5,
    )
    lim = srv.Limits.from_settings(s)
    assert lim.max_request_bytes == 8 * MB
    assert lim.max_response_bytes == 3 * MB
    assert lim.max_pages == 11
    assert lim.deadline_s == p.kill_deadline_s(100, 20) == 120
    assert lim.cpu_threads == 5


# --- the health probe ------------------------------------------------------------------------


def test_the_probe_is_true_for_a_live_parser_and_false_for_anything_else(sock_dir: Path) -> None:
    gone = sock_dir / "p.sock"
    egress.installed().allow_unix(gone)  # type: ignore[union-attr]
    assert srv.ping(gone, timeout_s=1) is False  # nothing there
    with Harness(sock_dir) as h:
        assert srv.ping(h.path, timeout_s=5) is True


def test_the_probe_is_false_for_a_peer_that_answers_something_else(sock_dir: Path) -> None:
    path = sock_dir / "liar.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def answer() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(100)
            p.send_frame(conn, {"ok": False, "code": "INTERNAL"})

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    try:
        assert srv.ping(path, timeout_s=5) is False
    finally:
        t.join(5)
        listener.close()


def test_the_probe_is_false_when_the_parser_is_busy_past_its_timeout(sock_dir: Path) -> None:
    release = threading.Event()

    def converter(path: Path, kind: str) -> srv.Converted:
        release.wait(10)
        return good_converter(path, kind)

    with Harness(sock_dir, converter) as h:
        busy = h.connect()
        p.send_frame(busy, {"op": "convert", "kind": "pdf"}, b"x")
        time.sleep(0.2)
        assert srv.ping(h.path, timeout_s=0.5) is False
        release.set()
        busy.close()


def test_the_server_module_hands_nothing_but_codes_to_the_wire() -> None:
    assert {c.name for c in p.ErrorCode} == {c.value for c in p.ErrorCode}


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


# --- the corners ----------------------------------------------------------------------------


def test_disarming_a_switch_that_was_never_armed_only_cancels_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Syscalls(monkeypatch)
    srv.KillSwitch().disarm()
    assert calls.alarms == [0] and calls.limits == []


def test_a_socket_that_cannot_be_bound_is_reported_and_the_umask_is_put_back(
    sock_dir: Path,
) -> None:
    import os

    locked = sock_dir / "locked"
    locked.mkdir(mode=0o500)
    before = os.umask(0o022)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("this user can write to a directory without permission (root)")
        with pytest.raises(PermissionError):
            srv.serve(
                convert_fn=good_converter,
                socket_path=locked / "p.sock",
                limits=limits(),
                stop=threading.Event(),
            )
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)
        locked.chmod(0o700)


def test_a_server_that_is_already_told_to_stop_binds_and_returns(sock_dir: Path) -> None:
    stop = threading.Event()
    stop.set()
    srv.serve(
        convert_fn=good_converter, socket_path=sock_dir / "p.sock", limits=limits(), stop=stop
    )
    assert not (sock_dir / "p.sock").exists()


def test_a_bug_in_the_service_is_an_internal_error_and_does_not_end_it(sock_dir: Path) -> None:
    class Broken:
        document = b"{}"
        status = "success"

        @property
        def pages(self) -> int:
            raise RuntimeError(CANARY)

    calls = [0]

    def converter(path: Path, kind: str) -> srv.Converted:
        calls[0] += 1
        return Broken() if calls[0] == 1 else good_converter(path, kind)  # type: ignore[return-value]

    with Harness(sock_dir, converter) as h:
        header, _ = h.convert()
        assert header == {"ok": False, "code": "INTERNAL"}
        assert h.convert()[0]["ok"] is True


def test_a_refused_request_is_not_read_to_the_end_for_ever(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_DRAIN_S", 0.0)
    with Harness(sock_dir, lim=limits(max_request_bytes=1000)) as h:
        with h.connect() as s:
            p.send_frame(s, {"op": "convert", "kind": "pdf"}, b"x" * 3000)
            header, _ = p.read_frame(
                s, limit=lambda x: 0, max_payload=0, deadline=time.monotonic() + 10
            )
        assert header == {"ok": False, "code": "TOO_LARGE"}
        assert h.call({"op": "ping"})[0]["ok"] is True


def test_with_a_deadline_and_no_guard_given_the_real_kill_switch_is_the_one_used(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[RecordingGuard] = []

    def factory() -> RecordingGuard:
        made.append(RecordingGuard())
        return made[0]

    monkeypatch.setattr(srv, "KillSwitch", factory)  # the real one would end this process
    with Harness(sock_dir, lim=limits(deadline_s=9)) as h:
        h.convert()
    assert len(made) == 1 and made[0].events == [("arm", 9), ("disarm", 0)]

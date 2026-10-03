"""The kill switch, for real: a process, a signal, and a conversion that never returns.

Everything else about the parser is tested with the service in a thread of the test process. This
is the one property that cannot be: ``SIGALRM`` at its default action ends the *whole process*, and
the point is that it does so even when the process is stuck in native code that holds the GIL. The
helper (``parser_helper.py``) is the real ``serve`` with the real ``KillSwitch``; its converter
waits inside a ``PyDLL`` call that does not return, which is what a native parser stuck in a page
looks like.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from procurement_agent.parser_service import protocol as p
from procurement_agent.security import egress

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tests" / "unit" / "parser_helper.py"


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    short = Path(tempfile.mkdtemp(prefix="pa-pk-"))
    yield short
    shutil.rmtree(short, ignore_errors=True)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PA_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def start(sock_dir: Path, mode: str, deadline: int) -> tuple[subprocess.Popen[bytes], Path]:
    path = sock_dir / "p.sock"
    egress.installed().allow_unix(path)  # type: ignore[union-attr]
    proc = subprocess.Popen(
        [sys.executable, str(HELPER), str(path), mode, str(deadline)],
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    end = time.monotonic() + 60
    while time.monotonic() < end:
        if path.exists():
            break
        assert proc.poll() is None, proc.stderr.read().decode()[-1500:]  # type: ignore[union-attr]
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("the helper never listened")
    return proc, path


def _request(
    path: Path, op: dict[str, object], payload: bytes = b"", wait: float = 30.0
) -> tuple[p.Header | None, float]:
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(wait)
        s.connect(str(path))
        p.send_frame(s, op, payload)
        try:
            header, _ = p.read_frame(
                s, limit=lambda h: 10**6, max_payload=10**6, deadline=time.monotonic() + wait
            )
        except (p.ProtocolError, OSError):
            return None, time.monotonic() - started
        return header, time.monotonic() - started


def test_a_conversion_wedged_in_native_code_is_ended_by_the_kernel_at_the_deadline(
    sock_dir: Path,
) -> None:
    proc, path = start(sock_dir, "hang", deadline=2)
    try:
        header, seconds = _request(path, {"op": "convert", "kind": "pdf"}, b"%PDF-1.4")
        assert header is None, "the peer saw the connection end, not an answer"
        assert 1.5 <= seconds < 8, f"killed after {seconds:.1f} s for a 2 s deadline"
        assert proc.wait(10) == -signal.SIGALRM, "ended by the signal itself, not by Python"
    finally:
        proc.kill()
        proc.wait(10)
        if proc.stderr:
            proc.stderr.close()
    assert path.exists(), "a killed server leaves its socket file: the next start replaces it"
    with pytest.raises(ConnectionRefusedError), socket.socket(socket.AF_UNIX) as s:
        s.connect(str(path))


def test_a_python_handler_would_not_have_helped(tmp_path: Path) -> None:
    """The control: with a handler on SIGALRM, the same native call outlives the alarm - the
    handler is Python, and Python does not run until the call returns. That is why the kill switch
    leaves the default action alone (and refuses to arm when it finds a handler)."""
    script = (
        "import ctypes, signal, time\n"
        "class TS(ctypes.Structure):\n"
        "    _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]\n"
        "fired = []\n"
        "signal.signal(signal.SIGALRM, lambda *a: fired.append(time.monotonic()))\n"
        "libc = ctypes.PyDLL(None)\n"
        "mutex = ctypes.create_string_buffer(64)\n"
        "libc.pthread_mutex_lock(mutex)\n"
        "start = time.monotonic()\n"
        "signal.alarm(1)\n"
        "libc.pthread_mutex_timedlock(mutex, ctypes.byref(TS(int(time.time()) + 4, 0)))\n"
        "print(round(fired[0] - start, 1), round(time.monotonic() - start, 1))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr[-500:]
    handled_at, finished_at = (float(x) for x in done.stdout.split())
    assert handled_at >= 2.5, "the handler ran only once the native call returned"
    assert finished_at >= 2.5


def test_the_cpu_limit_ends_a_process_whose_alarm_is_gone(sock_dir: Path) -> None:
    """The backstop on its own: arm, cancel the alarm, spin - the CPU limit (what had been used,
    plus the deadline times the threads) ends the process with SIGXCPU."""
    script = (
        "from procurement_agent.parser_service import server\n"
        "import signal, time\n"
        "k = server.KillSwitch(); k.install()\n"
        "k.arm(1, 1)\n"
        "signal.alarm(0)\n"
        "while True:\n"
        "    time.monotonic()\n"
    )
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", script], env=_env(), capture_output=True, timeout=60, check=False
    )
    assert done.returncode == -signal.SIGXCPU, done.stderr[-500:]
    assert time.monotonic() - started < 30


def test_a_conversion_that_finishes_in_time_disarms_the_alarm(sock_dir: Path) -> None:
    proc, path = start(sock_dir, "fine", deadline=2)
    try:
        header, _ = _request(path, {"op": "convert", "kind": "pdf"}, b"%PDF-1.4")
        assert header == {"ok": True, "op": "convert", "pages": 1, "status": "success"}
        time.sleep(3.0)  # past the deadline: a still-armed alarm would have ended the process
        assert proc.poll() is None
        header, _ = _request(path, {"op": "ping"})
        assert header == {"ok": True, "op": "ping"}
    finally:
        proc.kill()
        proc.wait(10)
        if proc.stderr:
            proc.stderr.close()


def test_the_next_conversion_gets_a_fresh_deadline(sock_dir: Path) -> None:
    proc, path = start(sock_dir, "fine", deadline=3)
    try:
        for _ in range(3):
            header, _ = _request(path, {"op": "convert", "kind": "pdf"}, b"%PDF-1.4")
            assert header is not None and header["ok"] is True
            time.sleep(1.2)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(10)
        if proc.stderr:
            proc.stderr.close()

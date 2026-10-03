"""The liveness supervisor against the real server, with the failure it exists for.

A request that spins in a regular expression or inside a native parser holds the GIL. From outside
the process that is indistinguishable from a process that was stopped: the kernel still accepts the
connection and nothing ever answers it. ``SIGSTOP`` produces exactly that - and, as a wedged
interpreter does, leaves ``SIGTERM`` pending, so the supervisor's last resort (``SIGKILL`` once
the grace period is over) is exercised as well. ``tests/unit/test_supervisor.py`` covers the
policy with stub servers; this is the same loop around ``uvicorn procurement_agent.main:app``.

What is asserted: ``/ready`` answers, then stops answering; the server is killed and started
again as a new process; ``/ready`` answers again; nothing is left running once the supervisor stops.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_supervisor() -> object:
    spec = importlib.util.spec_from_file_location("supervise", ROOT / "scripts" / "supervise.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["supervise"] = module
    spec.loader.exec_module(module)
    return module


supervise = _load_supervisor()
Policy = supervise.Policy  # type: ignore[attr-defined]
Supervisor = supervise.Supervisor  # type: ignore[attr-defined]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=1.0) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _events(out: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _started(out: io.StringIO) -> list[int]:
    return [int(str(e["pid"])) for e in _events(out) if e["event"] == "server_started"]


def _wait(condition: Callable[[], bool], timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_real_server_that_stops_answering_is_killed_and_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    python_path = os.pathsep.join(p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH")) if p)
    for name, value in {
        "PA_ENV": "dev",
        "PA_PARSER_MODE": "inline",
        "PA_DEV_API_KEY": "pa_supervisor_test_key_0123456789abcdef",
        "PA_DATA_DIR": str(tmp_path / "data"),
        "PA_OLLAMA_BASE_URL": "http://127.0.0.1:9",  # never called: /ready does not ask the model
        "PA_RATE_LIMIT": "100000/minute",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": python_path,
    }.items():
        monkeypatch.setenv(name, value)  # the supervisor's child inherits the environment
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "procurement_agent.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-server-header",
        "--log-level",
        "warning",
    ]
    # The start-up grace (the models take minutes to load in production) is measured by the
    # supervisor's clock; skewing it forward once the server is up turns what follows into a hang
    # rather than a slow start, without waiting out a real grace period.
    skew = [0.0]
    out = io.StringIO()
    supervisor = Supervisor(
        command,
        port,
        Policy(
            interval_s=0.5,
            failures=3,
            grace_s=60.0,
            stop_s=3.0,
            probe_timeout_s=1.0,
            max_restarts=3,
            window_s=600.0,
        ),
        clock=lambda: time.monotonic() + skew[0],
        out=out,
    )
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    try:
        assert _wait(lambda: _ready(port), 120), "the server never became ready"
        first = _started(out)[0]
        skew[0] = 10_000.0
        os.kill(first, signal.SIGSTOP)  # accepts connections (the kernel does), answers none
        assert _wait(lambda: not _ready(port), 10), "a stopped server still answered /ready"
        assert _wait(lambda: len(_started(out)) >= 2, 90), _events(out)
        second = _started(out)[1]
        assert second != first
        assert _wait(lambda: _ready(port), 120), "the restarted server never became ready"
        names = [str(e["event"]) for e in _events(out)]
        assert "restarting" in names, names
        assert "killed" in names, "SIGTERM cannot reach a stopped process: only SIGKILL ends it"
        assert not _alive(first)
    finally:
        supervisor.stop()
        thread.join(90)
    assert not thread.is_alive(), "the supervisor did not stop"
    assert not any(_alive(pid) for pid in _started(out)), "a server process was left running"
    assert not _ready(port)

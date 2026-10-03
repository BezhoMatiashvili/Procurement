"""The liveness supervisor: real child processes, a stub ``/ready``, and children that misbehave."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load() -> object:
    spec = importlib.util.spec_from_file_location("supervise", ROOT / "scripts" / "supervise.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["supervise"] = module
    spec.loader.exec_module(module)
    return module


supervise = _load()
Policy = supervise.Policy  # type: ignore[attr-defined]
Supervisor = supervise.Supervisor  # type: ignore[attr-defined]

#: a child that runs until it is told to stop
SLEEPER = [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"]
#: a child that ignores SIGTERM, as a process wedged in native code would
STUBBORN = [
    sys.executable,
    "-c",
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "while True: time.sleep(0.05)",
]
QUICK = Policy(interval_s=0.05, failures=3, grace_s=0.0, stop_s=0.5, probe_timeout_s=0.5)


class _Ready(BaseHTTPRequestHandler):
    status = 200

    def log_message(self, *_: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def ready_server() -> Iterator[tuple[int, type[_Ready]]]:
    handler = type("Handler", (_Ready,), {"status": 200})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], handler
    server.shutdown()
    server.server_close()


def _events(out: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _run_in_thread(sup: object) -> tuple[threading.Thread, list[int]]:
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(sup.run()))  # type: ignore[attr-defined]
    t.start()
    return t, result


def _wait(condition, timeout: float = 10.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


# --- the probe ---------------------------------------------------------------------------------


def test_the_probe_is_a_fixed_loopback_address() -> None:
    assert supervise.PROBE_HOST == "127.0.0.1"  # type: ignore[attr-defined]
    source = (ROOT / "scripts" / "supervise.py").read_text(encoding="utf-8")
    assert "PA_SUPERVISOR_HOST" not in source and 'environ["PA_OLLAMA' not in source
    assert re.findall(r"http://\{PROBE_HOST\}", source) == ["http://{PROBE_HOST}"]


def test_the_probe_is_true_only_for_a_200(ready_server: tuple[int, type[_Ready]]) -> None:
    port, handler = ready_server
    assert supervise.probe(port, 1.0) is True  # type: ignore[attr-defined]
    handler.status = 503
    assert supervise.probe(port, 1.0) is False  # type: ignore[attr-defined]
    assert supervise.probe(1, 0.2) is False  # nothing listens: refused


def test_a_server_that_accepts_and_never_answers_is_a_failed_probe() -> None:
    import socket

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    try:
        started = time.monotonic()
        assert supervise.probe(silent.getsockname()[1], 0.3) is False  # type: ignore[attr-defined]
        assert time.monotonic() - started < 3
    finally:
        silent.close()


# --- supervising a child -----------------------------------------------------------------------


def test_a_healthy_server_is_left_alone_and_stops_when_asked(
    ready_server: tuple[int, type[_Ready]],
) -> None:
    port, _ = ready_server
    out = io.StringIO()
    sup = Supervisor(SLEEPER, port, QUICK, out=out)
    thread, result = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
    time.sleep(0.4)  # many probes
    sup.stop()
    thread.join(10)
    assert result[0] in (0, -15, 143)  # the child's SIGTERM status, or 0
    names = [e["event"] for e in _events(out)]
    assert names.count("server_started") == 1
    assert "restarting" not in names and "not_ready" not in names


def test_a_server_that_stops_answering_is_restarted(
    ready_server: tuple[int, type[_Ready]],
) -> None:
    port, handler = ready_server
    out = io.StringIO()
    sup = Supervisor(SLEEPER, port, QUICK, out=out)
    thread, _ = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
    first_pid = next(e["pid"] for e in _events(out) if e["event"] == "server_started")
    handler.status = 503  # /ready now says the worker is stalled
    assert _wait(lambda: sum(e["event"] == "server_started" for e in _events(out)) >= 2)
    handler.status = 200
    sup.stop()
    thread.join(10)
    events = _events(out)
    second_pid = [e["pid"] for e in events if e["event"] == "server_started"][1]
    assert second_pid != first_pid
    restarting = [e for e in events if e["event"] == "restarting"]
    assert restarting and restarting[0]["reason"] == "not_ready"
    assert [e["consecutive"] for e in events if e["event"] == "not_ready"][:3] == [1, 2, 3]


def test_one_failed_probe_is_not_a_restart(ready_server: tuple[int, type[_Ready]]) -> None:
    port, handler = ready_server
    answers = iter([False, True, False, True, False, True, True, True])
    out = io.StringIO()
    sup = Supervisor(SLEEPER, port, QUICK, check=lambda *_: next(answers, True), out=out)
    thread, _ = _run_in_thread(sup)
    assert _wait(lambda: not list(answers) or len(_events(out)) >= 1)
    time.sleep(0.6)
    sup.stop()
    thread.join(10)
    assert "restarting" not in [e["event"] for e in _events(out)]
    assert handler.status == 200


def test_failures_during_the_start_up_grace_do_not_count() -> None:
    out = io.StringIO()
    policy = Policy(interval_s=0.05, failures=2, grace_s=60.0, stop_s=0.5)
    sup = Supervisor(SLEEPER, 1, policy, check=lambda *_: False, out=out)
    thread, _ = _run_in_thread(sup)
    time.sleep(0.5)
    sup.stop()
    thread.join(10)
    names = [e["event"] for e in _events(out)]
    assert "not_ready" not in names and "restarting" not in names


def test_a_server_that_exits_is_started_again() -> None:
    out = io.StringIO()
    flaky = [sys.executable, "-c", "import time; time.sleep(0.15)"]
    sup = Supervisor(
        flaky,
        1,
        Policy(interval_s=0.05, grace_s=60, stop_s=0.5, max_restarts=50),
        out=out,
        check=lambda *_: True,
    )
    thread, _ = _run_in_thread(sup)
    assert _wait(lambda: sum(e["event"] == "server_started" for e in _events(out)) >= 3)
    sup.stop()
    thread.join(10)
    assert [e["code"] for e in _events(out) if e["event"] == "server_exited"][:2] == [0, 0]


def test_a_crash_loop_ends_in_a_failure_instead_of_restarting_for_ever() -> None:
    out = io.StringIO()
    crash = [sys.executable, "-c", "raise SystemExit(3)"]
    policy = Policy(interval_s=0.02, grace_s=60, stop_s=0.5, max_restarts=3, window_s=60)
    sup = Supervisor(crash, 1, policy, out=out, check=lambda *_: True)
    assert sup.run() == 1
    events = _events(out)
    assert [e["event"] for e in events].count("server_started") == 4  # the first, then three more
    assert events[-1]["event"] == "giving_up" and events[-1]["restarts"] == 3


def test_a_stuck_server_that_ignores_sigterm_is_killed() -> None:
    out = io.StringIO()
    sup = Supervisor(
        STUBBORN,
        1,
        Policy(interval_s=0.05, failures=1, grace_s=0.0, stop_s=0.3),
        out=out,
        check=lambda *_: False,
    )
    thread, _ = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "killed" for e in _events(out)), 15)
    sup.stop()
    thread.join(15)
    killed = [e for e in _events(out) if e["event"] == "killed"]
    assert killed and isinstance(killed[0]["pid"], int)
    assert sum(e["event"] == "server_started" for e in _events(out)) >= 2  # and then started again


def test_giving_up_after_too_many_not_ready_restarts_is_a_failure() -> None:
    out = io.StringIO()
    policy = Policy(
        interval_s=0.02, failures=1, grace_s=0.0, stop_s=0.3, max_restarts=2, window_s=60
    )
    sup = Supervisor(SLEEPER, 1, policy, check=lambda *_: False, out=out)
    assert sup.run() == 1
    assert [e["event"] for e in _events(out)][-1] == "giving_up"


def test_the_log_lines_carry_no_free_text() -> None:
    out = io.StringIO()
    sup = Supervisor(
        SLEEPER,
        1,
        Policy(interval_s=0.02, failures=1, grace_s=0.0, stop_s=0.3, max_restarts=1),
        check=lambda *_: False,
        out=out,
    )
    sup.run()
    allowed = {"event", "pid", "code", "reason", "consecutive", "restarts", "window_s"}
    for event in _events(out):
        assert set(event) <= allowed, event
        assert all(
            isinstance(v, int | float) or re.fullmatch(r"[a-z_]+", str(v)) for v in event.values()
        )


# --- stopping ----------------------------------------------------------------------------------
# `docker compose stop app` measured 9.8, 10.9 and 15.2 s on an idle app. The signal handler only
# sets an Event, and the loop sat in `time.sleep(15)`, which Python resumes after a handler returns
# (PEP 475): the server got its SIGTERM up to a whole probe interval late.

#: far longer than any test waits: a stop that is not heard at once is a failure, not a slow pass
SLOW = Policy(interval_s=60.0, failures=3, grace_s=0.0, stop_s=5.0, probe_timeout_s=0.5)


def _stop_and_time(sup: object, thread: threading.Thread, limit: float = 10.0) -> float:
    began = time.monotonic()
    sup.stop()  # type: ignore[attr-defined]
    thread.join(limit)
    return time.monotonic() - began


def test_a_stop_is_heard_at_once_between_probes(ready_server: tuple[int, type[_Ready]]) -> None:
    port, _ = ready_server
    out = io.StringIO()
    sup = Supervisor(SLEEPER, port, SLOW, out=out)
    thread, result = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
    time.sleep(0.3)  # the loop is now in its wait for the next probe
    took = _stop_and_time(sup, thread)
    assert not thread.is_alive(), "the supervisor slept through the stop request"
    assert took < 3.0, took
    assert result[0] in (0, -15, 143)


def test_a_stop_during_the_start_up_grace_is_heard_at_once() -> None:
    out = io.StringIO()
    grace = Policy(interval_s=60.0, failures=1, grace_s=3600.0, stop_s=5.0)
    sup = Supervisor(SLEEPER, 1, grace, check=lambda *_: False, out=out)
    thread, _ = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
    time.sleep(0.3)
    assert _stop_and_time(sup, thread) < 3.0
    assert not thread.is_alive()
    assert "not_ready" not in [e["event"] for e in _events(out)]


def test_a_stop_while_the_server_ignores_sigterm_ends_with_the_kill_not_the_interval() -> None:
    out = io.StringIO()
    policy = Policy(interval_s=60.0, failures=3, grace_s=0.0, stop_s=0.5, probe_timeout_s=0.5)
    sup = Supervisor(STUBBORN, 1, policy, check=lambda *_: True, out=out)
    thread, result = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
    time.sleep(0.3)
    took = _stop_and_time(sup, thread)
    assert not thread.is_alive()
    assert 0.4 < took < 4.0, took  # the stop_s wait, then SIGKILL, then out
    assert [e["event"] for e in _events(out)].count("killed") == 1
    assert result[0] != 0  # a server that had to be killed is not a clean stop


def test_a_stop_while_a_probe_is_in_flight_is_not_held_up_by_the_probe() -> None:
    released = threading.Event()
    out = io.StringIO()
    policy = Policy(interval_s=0.05, failures=3, grace_s=0.0, stop_s=5.0, probe_timeout_s=60.0)

    def hanging_probe(*_: object) -> bool:
        released.wait(30)  # a /ready that does not answer, as a wedged server's does not
        return False

    sup = Supervisor(SLEEPER, 1, policy, check=hanging_probe, out=out)
    thread, _ = _run_in_thread(sup)
    try:
        assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
        time.sleep(0.3)  # the loop is inside the probe
        assert _stop_and_time(sup, thread) < 3.0
        assert not thread.is_alive()
        assert "not_ready" not in [e["event"] for e in _events(out)]  # a stop is not a failure
    finally:
        released.set()


def test_a_server_that_exits_while_a_stop_is_pending_is_not_started_again() -> None:
    out = io.StringIO()
    quick = [sys.executable, "-c", "import time; time.sleep(0.2)"]
    policy = Policy(interval_s=60.0, grace_s=60, stop_s=0.5, max_restarts=50)
    sup = Supervisor(quick, 1, policy, check=lambda *_: True, out=out)
    thread, _ = _run_in_thread(sup)
    assert _wait(lambda: any(e["event"] == "server_started" for e in _events(out)))
    sup.stop()
    thread.join(10)
    assert not thread.is_alive()
    assert [e["event"] for e in _events(out)].count("server_started") == 1


@pytest.mark.parametrize("grace", [0.0, 3600.0], ids=["after-grace", "in-grace"])
def test_sigterm_to_the_supervisor_process_stops_it_within_seconds(grace: float) -> None:
    """The real signal path: the script's own handler, in a process of its own."""
    import os
    import signal
    import subprocess

    runner = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('supervise', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['supervise'] = m\n"
        "spec.loader.exec_module(m)\n"
        "sys.exit(m.main(command=[sys.executable, '-c', sys.argv[2]]))\n"
    )
    env = {
        **os.environ,
        "PA_SUPERVISOR_INTERVAL_S": "60",
        "PA_SUPERVISOR_GRACE_S": str(grace or 1e-9),
        "PA_SUPERVISOR_STOP_S": "5",
    }
    # ends by itself, so a supervisor that failed this test cannot leave it running
    child = "import time\nend = time.time() + 60\nwhile time.time() < end: time.sleep(0.05)"
    script = str(ROOT / "scripts" / "supervise.py")
    proc = subprocess.Popen(
        [sys.executable, "-c", runner, script, child], env=env, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert "server_started" in proc.stdout.readline()
        time.sleep(0.5)  # into the wait between probes
        began = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("SIGTERM was not heard until the probe interval ran out")
        assert time.monotonic() - began < 3.0
        assert code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()


# --- configuration -----------------------------------------------------------------------------


def test_the_policy_reads_the_environment_and_ignores_nonsense() -> None:
    p = Policy.from_environment({"PA_SUPERVISOR_INTERVAL_S": "7", "PA_SUPERVISOR_FAILURES": "5"})
    assert (p.interval_s, p.failures) == (7.0, 5)
    for junk in ("abc", "-3", "0", ""):
        q = Policy.from_environment({"PA_SUPERVISOR_INTERVAL_S": junk})
        assert q.interval_s == Policy().interval_s
    assert Policy.from_environment({}) == Policy()


# --- in the image ------------------------------------------------------------------------------


def test_the_supervisor_runs_exactly_the_command_the_image_runs_without_it() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))  # type: ignore[union-attr]
    assert cmd == supervise.SERVER_COMMAND  # type: ignore[attr-defined]


def test_the_stop_timeout_fits_inside_composes_grace_period() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    grace = int(str(compose["services"]["app"]["stop_grace_period"]).rstrip("s"))
    assert Policy().stop_s < grace


def test_compose_starts_the_app_through_the_supervisor_and_the_image_contains_it() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["app"]["command"] == ["python", "/app/scripts/supervise.py"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY .*scripts/supervise\.py \./scripts/supervise\.py$", dockerfile, re.M)
    assert re.search(r"^WORKDIR /app$", dockerfile, re.M)  # so ./scripts/... is /app/scripts/...
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "!scripts/supervise.py" in ignore.splitlines()


def test_the_supervisor_imports_nothing_but_the_standard_library() -> None:
    import ast

    tree = ast.parse((ROOT / "scripts" / "supervise.py").read_text(encoding="utf-8"))
    modules = {
        (n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert not {m for m in modules if m not in sys.stdlib_module_names}, modules

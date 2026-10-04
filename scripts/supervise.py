#!/usr/bin/env python
"""Run the server as a child and restart it when it stops answering.

Why this exists: a request that makes the process spin inside a regular expression or a native
parser holds the GIL, and nothing *inside* the process - a watchdog thread included - can run to
notice. ``/ready`` then stops answering, ``docker ps`` says "unhealthy", and nothing restarts the
container, because Docker restarts on exit, not on a failed health check. This is the process
outside: it asks ``/ready`` over loopback every few seconds, and when the answer is missing or
negative several times in a row it stops the server and starts it again. A restart marks the job
that was running as failed (``Worker._recover``) and loses nothing else.

It is stdlib-only on purpose (it must run when the application's own imports are what is broken),
lives under ``scripts/`` because it needs ``subprocess`` - which ``src/`` never imports, by test -
and is copied into the image by the Dockerfile. It manages only the child it started; it never
looks up or signals any other process. The probe is a fixed loopback address.

    python scripts/supervise.py            # what docker-compose.yml runs

Settings (environment, all optional): PA_SUPERVISOR_INTERVAL_S (15), PA_SUPERVISOR_FAILURES (3),
PA_SUPERVISOR_GRACE_S (180: the model-loading start), PA_SUPERVISOR_STOP_S (50: below compose's
stop_grace_period of 60), PA_SUPERVISOR_MAX_RESTARTS (5 in PA_SUPERVISOR_WINDOW_S, 600; past that it
exits non-zero rather than restart for ever).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

#: The probe never leaves this machine: the address is not configurable.
PROBE_HOST = "127.0.0.1"
#: What the Dockerfile's CMD runs; ``tests/unit/test_supervisor.py`` keeps the two the same.
SERVER_COMMAND = [
    "uvicorn",
    "procurement_agent.main:app",
    "--host",
    "0.0.0.0",  # noqa: S104 - inside the container; compose publishes loopback only
    "--port",
    "8000",
    "--workers",
    "1",
    "--proxy-headers",
    "--no-server-header",
]


@dataclass(frozen=True)
class Policy:
    interval_s: float = 15.0
    failures: int = 3
    grace_s: float = 180.0
    stop_s: float = 50.0
    probe_timeout_s: float = 5.0
    max_restarts: int = 5
    window_s: float = 600.0

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> Policy:
        env = dict(os.environ if env is None else env)

        def number(name: str, default: float) -> float:
            try:
                value = float(env[name])
            except (KeyError, ValueError):
                return default
            return value if value > 0 else default

        return cls(
            interval_s=number("PA_SUPERVISOR_INTERVAL_S", cls.interval_s),
            failures=int(number("PA_SUPERVISOR_FAILURES", cls.failures)),
            grace_s=number("PA_SUPERVISOR_GRACE_S", cls.grace_s),
            stop_s=number("PA_SUPERVISOR_STOP_S", cls.stop_s),
            max_restarts=int(number("PA_SUPERVISOR_MAX_RESTARTS", cls.max_restarts)),
            window_s=number("PA_SUPERVISOR_WINDOW_S", cls.window_s),
        )


def probe(port: int, timeout_s: float) -> bool:
    """True when ``/ready`` answers 200. Anything else - refused, timed out, 503 - is "no"."""
    try:
        with urllib.request.urlopen(
            f"http://{PROBE_HOST}:{port}/ready", timeout=timeout_s
        ) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


class Supervisor:
    def __init__(
        self,
        command: Sequence[str],
        port: int = 8000,
        policy: Policy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        check: Callable[[int, float], bool] = probe,
        out: Any = None,
    ) -> None:
        self.command = list(command)
        self.port = port
        self.policy = policy or Policy()
        self._clock, self._check = clock, check
        self._out = out or sys.stdout
        self._child: subprocess.Popen[bytes] | None = None
        self._started_at = 0.0
        self._failures = 0
        self._restarts: list[float] = []
        self._stop = threading.Event()
        #: non-zero once the supervisor has given up, so an operator (or Docker) sees a failure
        self.exit_code = 0

    # --- lifecycle -----------------------------------------------------------------------------

    def stop(self) -> None:
        """Ask for an orderly shutdown (a signal handler calls this)."""
        self._stop.set()

    def run(self) -> int:
        self._start()
        while not self._stop.is_set():
            code = self._child.poll() if self._child else 0
            if code is not None:
                self._log("server_exited", code=code)
                if self._stop.is_set():
                    break  # asked to stop in the meantime: nothing to start again
                if not self._allowed_to_restart():
                    return 1
                self._start()
                continue
            self._watch()
            # Every wait is on the stop Event, not time.sleep: Python resumes a sleep after a
            # signal handler returns (PEP 475), so a SIGTERM would reach the server a whole
            # interval late - `docker compose stop` measured 9.8-15.2 s on an idle app.
            self._stop.wait(self.policy.interval_s)
        status = self._terminate()
        # A server that stops because we asked it to (SIGTERM) is a clean stop, not a failure;
        # giving up on a server that will not stay up is.
        clean = status in (0, -signal.SIGTERM, 128 + signal.SIGTERM)
        return self.exit_code or (0 if clean else status)

    # --- internals -------------------------------------------------------------------------------

    def _start(self) -> None:
        self._child = subprocess.Popen(self.command)
        self._started_at = self._clock()
        self._failures = 0
        self._log("server_started", pid=self._child.pid)

    def _watch(self) -> None:
        answered = self._probe()
        if answered is None:
            return  # a stop was asked for while the probe was waiting: not a failed probe
        if answered:
            self._failures = 0
            return
        if self._clock() - self._started_at < self.policy.grace_s:
            return  # still starting: the models take minutes to load
        self._failures += 1
        self._log("not_ready", consecutive=self._failures)
        if self._failures >= self.policy.failures:
            self._restart("not_ready")

    def _probe(self) -> bool | None:
        """One probe, in a thread so that a stop request is heard while it waits (a probe to a
        wedged server takes ``probe_timeout_s``). ``None``: stopped before it answered."""
        answer: list[bool] = []
        worker = threading.Thread(
            target=lambda: answer.append(self._check(self.port, self.policy.probe_timeout_s)),
            daemon=True,
        )
        worker.start()
        while True:
            worker.join(0.05)  # returns at once when the probe has answered
            if not worker.is_alive():
                return answer[0] if answer else False
            if self._stop.is_set():
                return None

    def _restart(self, reason: str) -> None:
        if not self._allowed_to_restart():
            self.exit_code = 1
            self._stop.set()
            return
        self._log("restarting", reason=reason)
        self._terminate()
        if not self._stop.is_set():  # a stop that arrived meanwhile is not followed by a start
            self._start()

    def _allowed_to_restart(self) -> bool:
        now = self._clock()
        self._restarts = [t for t in self._restarts if now - t < self.policy.window_s]
        if len(self._restarts) >= self.policy.max_restarts:
            self._log("giving_up", restarts=len(self._restarts), window_s=self.policy.window_s)
            return False
        self._restarts.append(now)
        return True

    def _terminate(self) -> int:
        """SIGTERM, wait ``stop_s``, then SIGKILL. Returns the child's exit code. The wait ends
        the moment the child does (``Popen.wait`` with a timeout), not at the next poll."""
        child = self._child
        if child is None or child.poll() is not None:
            return int(child.returncode or 0) if child else 0
        child.terminate()
        try:
            child.wait(timeout=self.policy.stop_s)
        except subprocess.TimeoutExpired:
            self._log("killed", pid=child.pid)
            child.kill()
            child.wait()
        return int(child.returncode or 0)

    def _log(self, event: str, **fields: object) -> None:
        print(json.dumps({"event": event, **fields}), file=self._out, flush=True)


def main(command: Sequence[str] = SERVER_COMMAND, port: int = 8000) -> int:
    supervisor = Supervisor(command, port, Policy.from_environment())
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: supervisor.stop())
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())

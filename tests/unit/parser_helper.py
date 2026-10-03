"""A parser service for the tests that need a real process: ``parser_helper SOCKET MODE DEADLINE``.

Not a test module (pytest collects ``test_*.py``) and not part of the product: it serves the real
``serve`` with the real ``KillSwitch`` and a converter that does what the mode says. ``hang`` stops
inside a native call that holds the GIL - ``PyDLL`` calls do not release it, and this one does not
return on a signal - which is exactly what
a native parser stuck in a page looks like from Python: no handler, no thread, no timeout of the
interpreter's can run until the call returns (here: never).
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

from procurement_agent.parser_service import server

MB = 1024 * 1024


def hang(path: Path, kind: str) -> server.Converted:
    """Lock a non-recursive mutex twice: the thread waits for itself, in C, and a signal does not
    wake it (the C library retries the wait after a handler returns)."""
    libc = ctypes.PyDLL(None)  # a PyDLL call keeps the GIL
    mutex = ctypes.create_string_buffer(64)  # all zeros is an unlocked default mutex
    libc.pthread_mutex_lock(mutex)
    libc.pthread_mutex_lock(mutex)
    raise AssertionError("the wait ended")


def spin(path: Path, kind: str) -> server.Converted:
    while True:  # CPU, in bytecode: the wall clock alarm and the CPU limit both apply
        time.monotonic()


def fine(path: Path, kind: str) -> server.Converted:
    return server.Converted(
        pages=1, status="success", document=b'{"schema_name": "DoclingDocument"}'
    )


MODES = {"hang": hang, "spin": spin, "fine": fine}


def main() -> None:
    socket_path, mode, deadline = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    kill = server.KillSwitch()
    kill.install()
    limits = server.Limits(
        max_request_bytes=MB,
        max_response_bytes=MB,
        max_pages=300,
        deadline_s=deadline,
        cpu_threads=1,
        read_timeout_s=10.0,
        write_timeout_s=10.0,
    )
    server.serve(convert_fn=MODES[mode], socket_path=socket_path, limits=limits, guard=kill)


if __name__ == "__main__":
    main()

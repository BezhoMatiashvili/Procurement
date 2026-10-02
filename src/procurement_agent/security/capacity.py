"""Bounded concurrency for the work a request can make the server repeat.

Every report - the act check, a comparison, a bill of quantities, and each download of one - is
recomputed from the stored documents when it is asked for, on the web server's thread pool, which
every other request shares. One person reloading a heavy report, or a script asking for forty at
once, held forty threads and the single process's GIL, and everyone else (``/ready`` included)
waited behind them. The request rate limit counts requests, not the work each one costs.

``Capacity`` is a counting semaphore with a short wait: when every slot is taken the request is
answered at once with 503 and ``Retry-After``, instead of queuing without limit.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

__all__ = ["Capacity", "report_capacity"]


class Capacity:
    def __init__(self, slots: int, *, wait_s: float = 2.0) -> None:
        self.slots = slots
        self.wait_s = wait_s
        self._sem = threading.BoundedSemaphore(slots)

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._sem.acquire(timeout=self.wait_s):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "the server is busy with other reports; try again in a few seconds",
                headers={"Retry-After": "5"},
            )
        try:
            yield
        finally:
            self._sem.release()


_capacities: dict[tuple[int, float], Capacity] = {}
_lock = threading.Lock()


def report_capacity(slots: int, wait_s: float = 2.0) -> Capacity:
    """The process's one ``Capacity`` for reports of this size (created on first use)."""
    with _lock:
        return _capacities.setdefault((slots, wait_s), Capacity(slots, wait_s=wait_s))

"""A small fixed-window rate limiter.

This replaces slowapi, which was configured here but never limited anything. Its middleware
discovers the handler for a request by walking ``app.routes`` and looking for an object with an
``endpoint`` attribute; the FastAPI version in this project wraps routers included with
``include_router`` in a private object that has no such attribute, so no handler is found, and
a request with no handler is treated as *exempt*. The limiter, the 429 handler and the
configured limit were all present and the effective limit was unlimited.

A security control that fails open when a dependency changes its internals is not one we want
to keep. This implementation is about fifty lines, has no route discovery, and is covered by a
test that would fail if it ever stopped limiting.

Fixed windows are deliberate: they are the cheapest thing that bounds abuse, and this is an
internal tool behind API keys, not a public endpoint that needs smoothing.

Callers are identified by their address, not by the credential they present. The limiter runs
before authentication, so a key or cookie taken from the request is attacker-chosen: keying on
it gave every invented header a fresh bucket, which is no limit at all. Behind the reverse
proxy the address is only meaningful once uvicorn trusts ``X-Forwarded-For`` from that proxy
(``--forwarded-allow-ips`` / ``FORWARDED_ALLOW_IPS``) and from nowhere else.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$", re.IGNORECASE)
_WINDOW_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

#: Paths that must stay reachable for orchestration even under load.
EXEMPT_PREFIXES = ("/health", "/ready", "/static/")
#: Stop the bucket table from growing without bound under a spray of distinct addresses.
MAX_BUCKETS = 10_000


class RateLimitConfig:
    def __init__(self, limit: str) -> None:
        m = _LIMIT_RE.match(limit)
        if not m:
            msg = f"invalid rate limit {limit!r}; expected e.g. '120/minute'"
            raise ValueError(msg)
        self.max_requests = int(m.group(1))
        if self.max_requests < 1:
            msg = f"invalid rate limit {limit!r}; zero requests would refuse every call"
            raise ValueError(msg)
        self.window_s = _WINDOW_SECONDS[m.group(2).lower()]

    def __repr__(self) -> str:
        return f"RateLimitConfig({self.max_requests}/{self.window_s}s)"


def client_key(request: Request) -> str:
    """Identify the caller by address - the one thing about a request it cannot invent.

    See the module docstring for why the API key and session cookie are not used here.
    """
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimiter:
    """Fixed-window counters in a bounded LRU table.

    Every operation is O(1): the bucket is looked up, moved to the most-recent end and, when
    the table is full, the least recently seen caller is dropped. The previous prune rebuilt
    the whole dict and kept every bucket of the current window, so a spray of 10k addresses
    inside one window made every later request copy 10k entries while holding the lock.
    Evicting the least recently seen caller cannot help an attacker: an active caller is by
    definition recent, so it keeps its bucket and its 429.
    """

    def __init__(self, config: RateLimitConfig, *, max_buckets: int = MAX_BUCKETS) -> None:
        self._config = config
        self._max_buckets = max_buckets
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def check(self, key: str, now: float | None = None) -> float | None:
        """Return None when allowed, or the seconds until the window resets when not."""
        now = time.monotonic() if now is None else now
        window = self._config.window_s
        start = now - (now % window)
        with self._lock:
            bucket_start, count = self._buckets.get(key, (start, 0))
            if bucket_start < start:
                bucket_start, count = start, 0
            if count >= self._config.max_requests:
                self._buckets.move_to_end(key)
                return max(0.0, bucket_start + window - now)
            self._buckets[key] = (bucket_start, count + 1)
            self._buckets.move_to_end(key)
            if len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
        return None


def rate_limit_middleware(
    limiter: RateLimiter,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)
        retry_after = limiter.check(client_key(request))
        if retry_after is not None:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        return await call_next(request)

    return middleware

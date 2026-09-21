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
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$", re.IGNORECASE)
_WINDOW_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

#: Paths that must stay reachable for orchestration even under load.
EXEMPT_PREFIXES = ("/health", "/static/")
#: Stop the bucket dictionary from growing without bound under a spray of distinct keys.
_MAX_BUCKETS = 10_000


class RateLimitConfig:
    def __init__(self, limit: str) -> None:
        m = _LIMIT_RE.match(limit)
        if not m:
            msg = f"invalid rate limit {limit!r}; expected e.g. '120/minute'"
            raise ValueError(msg)
        self.max_requests = int(m.group(1))
        self.window_s = _WINDOW_SECONDS[m.group(2).lower()]

    def __repr__(self) -> str:
        return f"RateLimitConfig({self.max_requests}/{self.window_s}s)"


def client_key(request: Request) -> str:
    """Identify the caller.

    Prefer the API key prefix over the address: behind a reverse proxy every request shares one
    source IP, which would make a per-IP limit either useless or a denial of service against
    every user at once. Only the non-secret prefix is used, and it never reaches a log.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key[:11]}"
    session = request.cookies.get("pa_session")
    if session:
        return f"sess:{session[:11]}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimiter:
    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, int]] = {}

    def check(self, key: str, now: float | None = None) -> float | None:
        """Return None when allowed, or the seconds until the window resets when not."""
        now = time.monotonic() if now is None else now
        window = self._config.window_s
        start = now - (now % window)
        with self._lock:
            if len(self._buckets) > _MAX_BUCKETS:
                self._buckets = {k: v for k, v in self._buckets.items() if v[0] >= start}
            bucket_start, count = self._buckets.get(key, (start, 0))
            if bucket_start < start:
                bucket_start, count = start, 0
            if count >= self._config.max_requests:
                return max(0.0, bucket_start + window - now)
            self._buckets[key] = (bucket_start, count + 1)
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

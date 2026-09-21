"""The rate limiter. Covered directly because the one it replaced failed open silently."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from procurement_agent.security.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    client_key,
)


def _request(headers: dict[str, str] | None = None, host: str = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (host, 1234), "path": "/api/v1/x"})


@pytest.mark.parametrize(
    ("text", "requests", "window"),
    [("120/minute", 120, 60), ("5/second", 5, 1), ("1000/hour", 1000, 3600), ("10/day", 10, 86400)],
)
def test_limit_strings_parse(text: str, requests: int, window: int) -> None:
    cfg = RateLimitConfig(text)
    assert (cfg.max_requests, cfg.window_s) == (requests, window)


@pytest.mark.parametrize("text", ["", "120", "120/fortnight", "abc/minute", "-1/minute"])
def test_a_bad_limit_string_is_rejected_at_startup(text: str) -> None:
    with pytest.raises(ValueError, match="invalid rate limit"):
        RateLimitConfig(text)


def test_requests_are_allowed_up_to_the_limit_then_refused() -> None:
    limiter = RateLimiter(RateLimitConfig("3/minute"))
    assert [limiter.check("k", now=100.0) for _ in range(3)] == [None, None, None]
    assert limiter.check("k", now=100.0) is not None


def test_the_window_resets() -> None:
    limiter = RateLimiter(RateLimitConfig("2/minute"))
    for _ in range(2):
        limiter.check("k", now=100.0)
    assert limiter.check("k", now=100.0) is not None
    assert limiter.check("k", now=160.0) is None


def test_retry_after_counts_down_within_the_window() -> None:
    """Windows are fixed, so both probes must sit inside the same one: at now=100 the window
    is [60, 120)."""
    limiter = RateLimiter(RateLimitConfig("1/minute"))
    limiter.check("k", now=100.0)
    early = limiter.check("k", now=100.0)
    late = limiter.check("k", now=115.0)
    assert early == pytest.approx(20.0)
    assert late == pytest.approx(5.0)


def test_callers_are_limited_independently() -> None:
    limiter = RateLimiter(RateLimitConfig("1/minute"))
    assert limiter.check("a", now=1.0) is None
    assert limiter.check("b", now=1.0) is None
    assert limiter.check("a", now=1.0) is not None


def test_the_api_key_identifies_the_caller_not_the_address() -> None:
    """Behind a reverse proxy every request shares one source address, so a per-address limit
    would throttle every user at once."""
    a = client_key(_request({"X-API-Key": "pa_aaaaaaaaaaaa_rest"}))
    b = client_key(_request({"X-API-Key": "pa_bbbbbbbbbbbb_rest"}))
    assert a != b and a.startswith("key:")


def test_only_the_non_secret_prefix_of_a_key_is_used() -> None:
    secret = "pa_abcdefghijklmnopqrstuvwxyz"
    assert secret not in client_key(_request({"X-API-Key": secret}))


def test_anonymous_callers_fall_back_to_the_address() -> None:
    assert client_key(_request()) == "ip:10.0.0.1"


def test_bucket_count_stays_bounded() -> None:
    limiter = RateLimiter(RateLimitConfig("1/second"))
    for i in range(12_000):
        limiter.check(f"k{i}", now=1.0)
    # a later window evicts the stale entries rather than growing forever
    limiter.check("fresh", now=10_000.0)
    assert len(limiter._buckets) < 12_000

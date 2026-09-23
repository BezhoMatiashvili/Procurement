"""Hardening of the web layer that needs no running app: settings validation and the limiter."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from procurement_agent.config import Settings
from procurement_agent.security.ratelimit import RateLimitConfig, RateLimiter, client_key

# --- the LLM endpoint must stay on the private network -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://ollama:11434",
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://host.docker.internal:11434",
        "http://10.1.2.3:11434",
        "http://172.16.0.5:11434",
        "http://172.31.255.1:11434",
        "http://192.168.1.20:11434",
        "http://[::1]:11434",
        "http://[fd00::1]:11434",
        "http://gpu-box.lan:11434",
        "http://llm.internal:11434",
        "https://ollama.local",
    ],
)
def test_internal_llm_endpoints_are_accepted(url: str) -> None:
    assert Settings(ollama_base_url=url).ollama_base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        # the old prefix check accepted every one of these
        "http://10.evil.example:11434",
        "http://192.168.attacker.net:11434",
        "http://127.0.0.1.nip.io:11434",
        "http://172.217.0.1:11434",  # public 172.x outside 172.16/12
        "http://172.15.0.1:11434",
        "http://172.32.0.1:11434",
        # userinfo is not the host
        "http://127.0.0.1@evil.example:11434",
        # private by Python's definition, but not somewhere an LLM lives
        "http://169.254.169.254",  # cloud metadata
        "http://0.0.0.0:11434",
        "http://[::ffff:8.8.8.8]:11434",
        "http://8.8.8.8:11434",
        "http://ollama.example.com:11434",
        "ftp://ollama:11434",
        "http://:11434",
    ],
)
def test_public_or_ambiguous_llm_endpoints_are_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="internal host"):
        Settings(ollama_base_url=url)


# --- the rate limiter ----------------------------------------------------------------------


def _request(headers: dict[str, str] | None = None, host: str = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (host, 1234), "path": "/api/v1/x"})


def test_an_unvalidated_header_does_not_buy_a_fresh_bucket() -> None:
    """The key was taken from X-API-Key/cookie before anything checked them, so a random
    header per request meant a new bucket per request and no limit at all."""
    keys = {client_key(_request({"X-API-Key": f"pa_{i:08d}xxxxxxxx"})) for i in range(50)}
    keys |= {client_key(_request({"Cookie": f"pa_session=junk{i}"})) for i in range(50)}
    assert keys == {client_key(_request())}


def test_the_limit_holds_against_a_spray_of_invented_keys() -> None:
    limiter = RateLimiter(RateLimitConfig("5/minute"))
    verdicts = [
        limiter.check(client_key(_request({"X-API-Key": f"pa_{i:08d}rest"})), now=100.0)
        for i in range(10)
    ]
    assert verdicts[:5] == [None] * 5
    assert all(v is not None for v in verdicts[5:])


def test_a_bucket_spray_inside_one_window_stays_bounded_and_cheap() -> None:
    """Every key in the spray is current, so the old prune kept all of them and then copied
    the whole dict on every later request, under the lock."""
    limiter = RateLimiter(RateLimitConfig("100/minute"), max_buckets=1_000)
    for i in range(5_000):
        limiter.check(f"ip:{i}", now=100.0)
    assert len(limiter._buckets) <= 1_000
    started = time.perf_counter()
    for i in range(5_000, 25_000):
        limiter.check(f"ip:{i}", now=100.0)
    # O(1) per request: 20k checks against a full table in well under a second
    assert time.perf_counter() - started < 1.0
    assert len(limiter._buckets) <= 1_000


def test_the_most_recent_caller_survives_eviction() -> None:
    """LRU, not FIFO: an active abuser keeps their bucket (and their 429) under a spray."""
    limiter = RateLimiter(RateLimitConfig("2/minute"), max_buckets=10)
    limiter.check("abuser", now=100.0)
    limiter.check("abuser", now=100.0)
    for i in range(50):
        limiter.check(f"noise{i}", now=100.0)
        limiter.check("abuser", now=100.0)
    assert limiter.check("abuser", now=100.0) is not None

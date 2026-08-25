"""Unit tests for auth and rate limiting."""

from __future__ import annotations

import time

import pytest

from claude_gateway.security import ApiKeyAuth, AuthError, RateLimiter, RateLimitExceeded


def test_auth_disabled_when_no_key():
    auth = ApiKeyAuth("")
    assert not auth.enabled
    assert auth.authenticate(None, None) == "anonymous"


def test_auth_bearer_and_xapikey():
    auth = ApiKeyAuth("secret")
    assert auth.enabled
    owner = auth.authenticate("Bearer secret", None)
    assert owner.startswith("key_")
    assert auth.authenticate(None, "secret") == owner  # same owner id


def test_auth_rejects_bad_key():
    auth = ApiKeyAuth("secret")
    with pytest.raises(AuthError):
        auth.authenticate("Bearer nope", None)
    with pytest.raises(AuthError):
        auth.authenticate(None, None)


def test_rate_limiter_allows_then_blocks():
    rl = RateLimiter(per_min=60, burst=3)
    for _ in range(3):
        rl.check("ip1")  # burst capacity
    with pytest.raises(RateLimitExceeded):
        rl.check("ip1")
    # A different identity has its own bucket.
    rl.check("ip2")


def test_rate_limiter_refills():
    rl = RateLimiter(per_min=600, burst=1)  # 10 tokens/sec
    rl.check("a")
    with pytest.raises(RateLimitExceeded):
        rl.check("a")
    time.sleep(0.2)  # ~2 tokens refilled
    rl.check("a")


def test_rate_limiter_disabled():
    rl = RateLimiter(per_min=0, burst=0)
    for _ in range(100):
        rl.check("x")  # never raises

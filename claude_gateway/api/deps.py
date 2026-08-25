"""FastAPI dependencies: container access, auth, and rate limiting."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from ..container import Container
from ..security import AuthError, RateLimitExceeded


def get_container(request: Request) -> Container:
    return request.app.state.container


def client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Authenticate, enforce the rate limit, return the owner id."""
    container = get_container(request)
    ip = client_ip(request)
    try:
        owner = container.auth.authenticate(authorization, x_api_key)
    except AuthError as e:
        container.audit.record(
            "auth_failure", client=ip, outcome="denied", reason=str(e),
            path=request.url.path,
        )
        raise HTTPException(status_code=401, detail=str(e)) from e

    identity = owner if owner != "anonymous" else ip
    try:
        container.ratelimiter.check(identity)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(int(e.retry_after) + 1)},
        ) from e
    return owner

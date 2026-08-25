"""FastAPI application factory + middleware."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, get_settings
from .container import build_container, shutdown_container
from .logging_config import configure_logging, get_logger, log_kv, request_id_var
import logging

log = get_logger("gateway.app")

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

DESCRIPTION = """
Local Claude Code Gateway — exposes a locally-installed Claude Code (via the
Claude Agent SDK) as a localhost HTTP API.

* **REST**: `/v1/chat`, `/v1/agent`, `/v1/code`, `/v1/review`, `/v1/judge`
* **Streaming (SSE)**: `/v1/stream/{job_id}` or `stream=true` on any POST
* **WebSocket**: `/ws/jobs/{job_id}`
* **Sessions** survive restarts; **jobs** track usage and cost.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    container = await build_container(settings)
    app.state.container = container
    log.info("gateway %s listening on http://%s:%s", __version__, settings.host, settings.port)
    try:
        yield
    finally:
        await shutdown_container(container)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title="Claude Code Gateway",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Job-Id", "X-Session-Id", "X-Request-Id"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request_id_var.set(rid)
        # Localhost-only enforcement (defense in depth beyond bind address).
        if settings.localhost_only and not settings.allow_remote:
            host = request.client.host if request.client else ""
            if host not in _LOOPBACK:
                return JSONResponse(
                    status_code=403,
                    content={"error": "remote access disabled (localhost-only mode)"},
                )
        start = time.time()
        try:
            response = await call_next(request)
        except Exception as e:  # pragma: no cover
            log.exception("unhandled error: %s", e)
            return JSONResponse(
                status_code=500,
                content={"error": "internal server error", "detail": str(e)},
                headers={"X-Request-Id": rid},
            )
        dur_ms = int((time.time() - start) * 1000)
        response.headers["X-Request-Id"] = rid
        if request.url.path not in ("/v1/health", "/metrics", "/dashboard", "/"):
            log_kv(
                logging.getLogger("gateway.access"), logging.INFO, "request",
                method=request.method, path=request.url.path,
                status=response.status_code, ms=dur_ms,
            )
        return response

    # Routers
    from .api import (
        routes_chat,
        routes_jobs,
        routes_meta,
        routes_review,
        routes_sessions,
        routes_ws,
    )
    from .dashboard import routes as dashboard_routes

    app.include_router(routes_meta.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_review.router)
    app.include_router(routes_sessions.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_ws.router)
    app.include_router(dashboard_routes.router)
    return app

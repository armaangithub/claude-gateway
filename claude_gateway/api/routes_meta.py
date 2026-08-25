"""Meta endpoints: /v1/models, /v1/health, /v1/metrics."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from .. import __version__
from ..models import HealthResponse, ModelsResponse
from .deps import get_container

router = APIRouter(tags=["meta"])


@router.get("/v1/models", response_model=ModelsResponse)
async def list_models(request: Request) -> ModelsResponse:
    container = get_container(request)
    return ModelsResponse(data=container.models)


@router.get("/v1/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    container = get_container(request)
    info = container.backend.version_info()
    available = await container.backend.is_available()
    cli_version = None
    if container.backend.name == "cli":
        cli_version = await container.backend.cli_version()  # type: ignore[attr-defined]
    warm = getattr(container.backend, "warm_session_ids", lambda: [])()
    active_sessions = await container.db.count("sessions")
    return HealthResponse(
        status="ok" if available else "degraded",
        version=__version__,
        backend=container.backend.name,
        backend_available=available,
        sdk_version=info.get("sdk_version"),
        cli_version=cli_version or info.get("bundled_cli_version"),
        active_sessions=active_sessions,
        warm_sessions=len(warm),
        running_jobs=container.jobs.running_count(),
        uptime_s=round(time.time() - container.start_time, 1),
    )


@router.get("/v1/metrics")
async def metrics_json(request: Request) -> dict:
    container = get_container(request)
    snap = container.metrics.snapshot()
    snap["usage_totals"] = await container.db.usage_totals()
    return snap


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_prometheus(request: Request) -> str:
    """Prometheus exposition format."""
    container = get_container(request)
    return container.metrics.prometheus()

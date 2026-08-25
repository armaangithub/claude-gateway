"""Runtime container — builds and holds all wired components.

Kept separate from ``app.py`` so route modules can import the ``Container`` type
without a circular dependency on the FastAPI app factory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .backend import build_backend
from .backend.base import ClaudeBackend
from .config import Settings
from .jobs import JobManager
from .logging_config import get_logger
from .models import ModelInfo
from .observability import init_otel, metrics_registry
from .security import ApiKeyAuth, AuditLogger, RateLimiter
from .sessions import SessionManager
from .storage import Database
from .streaming import EventBroker

log = get_logger("gateway.container")


# Static catalog surfaced at /v1/models. The SDK ultimately resolves aliases.
MODEL_CATALOG = [
    ModelInfo(
        id="claude-opus-4-8",
        aliases=["opus", "opusplan"],
        description="Most capable Claude model (Opus 4.8).",
    ),
    ModelInfo(
        id="claude-sonnet-4-6",
        aliases=["sonnet"],
        description="Balanced speed/quality (Sonnet 4.6).",
    ),
    ModelInfo(
        id="claude-haiku-4-5",
        aliases=["haiku"],
        description="Fastest, lowest cost (Haiku 4.5).",
    ),
]


@dataclass
class Container:
    settings: Settings
    db: Database
    backend: ClaudeBackend
    broker: EventBroker
    sessions: SessionManager
    jobs: JobManager
    metrics: Any
    auth: ApiKeyAuth
    ratelimiter: RateLimiter
    audit: AuditLogger
    models: list[ModelInfo]
    start_time: float


async def build_container(settings: Settings) -> Container:
    settings.ensure_dirs()
    init_otel(settings.otel_enabled, settings.otel_endpoint, settings.service_name)

    db = Database(settings.resolved_db_path())
    await db.connect()

    backend = await build_backend(settings)

    broker = EventBroker()
    sessions = SessionManager(db, settings, backend)
    jobs = JobManager(db, backend, broker, sessions, settings, metrics_registry)
    auth = ApiKeyAuth(settings.api_key)
    ratelimiter = RateLimiter(settings.rate_limit_per_min, settings.rate_limit_burst)
    audit = AuditLogger(settings.audit_log_path())

    log.info(
        "container ready: backend=%s auth=%s rate_limit=%s/min",
        backend.name,
        "on" if auth.enabled else "OFF",
        settings.rate_limit_per_min,
    )
    return Container(
        settings=settings,
        db=db,
        backend=backend,
        broker=broker,
        sessions=sessions,
        jobs=jobs,
        metrics=metrics_registry,
        auth=auth,
        ratelimiter=ratelimiter,
        audit=audit,
        models=MODEL_CATALOG,
        start_time=time.time(),
    )


async def shutdown_container(container: Container) -> None:
    await container.jobs.aclose()
    await container.backend.aclose()
    await container.db.close()
    log.info("container shut down")

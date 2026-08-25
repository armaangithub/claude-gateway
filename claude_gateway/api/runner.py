"""Helpers shared by the route modules: SSE formatting and job submission."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..backend.base import RunConfig
from ..container import Container
from ..jobs import JobHandle


def format_sse(event: dict[str, Any]) -> str:
    """Render one event as a Server-Sent-Events frame."""
    etype = event.get("type", "message")
    seq = event.get("seq")
    payload = json.dumps(event, default=str)
    lines: list[str] = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {etype}")
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


async def stream_job_sse(container: Container, job_id: str) -> AsyncIterator[str]:
    """Stream a job's events as SSE — live from the broker, or replayed from DB
    if the in-memory channel has already been swept."""
    channel = container.broker.get(job_id)
    if channel is not None:
        async for ev in channel.subscribe(replay=True):
            yield format_sse(ev)
        return
    # Channel gone — replay persisted events.
    events = await container.db.list_events(job_id)
    if not events:
        yield format_sse({"type": "job.failed", "data": {"error": "unknown job"}})
        return
    for e in events:
        yield format_sse(
            {"type": e["type"], "seq": e["seq"], "ts": e["ts"], "data": e["data"]}
        )


async def submit_and_wait(
    container: Container,
    *,
    kind: str,
    prompt: str,
    config: RunConfig,
    session_id: str | None = None,
    resume: bool = False,
    stateless: bool = False,
    owner: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    handle = await container.jobs.submit(
        kind=kind, prompt=prompt, config=config, session_id=session_id,
        resume=resume, stateless=stateless, owner=owner, meta=meta,
    )
    # Wait a bit longer than the job's own timeout; None = wait indefinitely
    # (the job itself will still finalize on completion/error).
    job_timeout = container.settings.effective_job_timeout()
    wait_timeout = (job_timeout + 30) if job_timeout is not None else None
    return await container.jobs.wait(handle.job_id, timeout=wait_timeout)


async def submit_job(
    container: Container,
    *,
    kind: str,
    prompt: str,
    config: RunConfig,
    session_id: str | None = None,
    resume: bool = False,
    stateless: bool = False,
    owner: str | None = None,
    meta: dict[str, Any] | None = None,
) -> JobHandle:
    return await container.jobs.submit(
        kind=kind, prompt=prompt, config=config, session_id=session_id,
        resume=resume, stateless=stateless, owner=owner, meta=meta,
    )

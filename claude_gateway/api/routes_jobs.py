"""Job endpoints: list, get, cancel, and the SSE stream /v1/stream/{job_id}."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..models import JobView, Usage
from .deps import get_container, require_auth
from .runner import stream_job_sse

router = APIRouter(tags=["jobs"])


def _job_view(row: dict[str, Any]) -> JobView:
    usage = Usage(
        input_tokens=row.get("input_tokens", 0),
        output_tokens=row.get("output_tokens", 0),
        cache_read_input_tokens=row.get("cache_read_tokens", 0),
        cache_creation_input_tokens=row.get("cache_creation_tokens", 0),
        total_tokens=row.get("total_tokens", 0),
    )
    return JobView(
        job_id=row["job_id"],
        session_id=row.get("session_id"),
        kind=row.get("kind", "chat"),
        status=row.get("status", "QUEUED"),
        prompt=row.get("prompt", ""),
        response=row.get("response"),
        error=row.get("error"),
        usage=usage,
        cost_usd=row.get("cost_usd"),
        duration_ms=row.get("duration_ms"),
        num_turns=row.get("num_turns"),
        created_at=row.get("created_at", 0.0),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        backend=row.get("backend"),
    )


def _owns(row: dict, owner: str) -> bool:
    return owner == "anonymous" or row.get("owner") in (owner, None)


@router.get("/v1/jobs", response_model=list[JobView])
async def list_jobs(
    request: Request,
    owner: str = Depends(require_auth),
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    container = get_container(request)
    scope = None if owner == "anonymous" else owner
    rows = await container.jobs.list(
        session_id=session_id, status=status, owner=scope, limit=limit, offset=offset
    )
    return [_job_view(r) for r in rows]


@router.get("/v1/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: str, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    row = await container.db.get_job(job_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="job not found")
    return _job_view(row)


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    row = await container.db.get_job(job_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="job not found")
    ok = await container.jobs.cancel(job_id)
    container.audit.record("job_cancel", owner=owner, job=job_id, outcome="ok" if ok else "noop")
    return {"cancelled": ok, "job_id": job_id}


@router.get("/v1/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    row = await container.db.get_job(job_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "events": await container.db.list_events(job_id)}


@router.get("/v1/stream/{job_id}")
async def stream_job(job_id: str, request: Request, owner: str = Depends(require_auth)):
    """Stream a job's events as Server-Sent-Events (live or replayed)."""
    container = get_container(request)
    row = await container.db.get_job(job_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="job not found")
    return StreamingResponse(
        stream_job_sse(container, job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

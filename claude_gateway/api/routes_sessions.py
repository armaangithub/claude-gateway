"""Session management endpoints: /v1/sessions (+ messages)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..models import CreateSessionRequest, SessionView
from .deps import client_ip, get_container, require_auth

router = APIRouter(tags=["sessions"], prefix="/v1/sessions")


def _owns(row: dict, owner: str) -> bool:
    # When auth is disabled everyone is 'anonymous' and owns everything.
    return owner == "anonymous" or row.get("owner") in (owner, None)


@router.post("", response_model=SessionView)
async def create_session(
    req: CreateSessionRequest, request: Request, owner: str = Depends(require_auth)
):
    container = get_container(request)
    row = await container.sessions.create(
        kind=req.kind, title=req.title, workspace=req.workspace,
        model=req.model, system_prompt=req.system_prompt, owner=owner,
    )
    container.audit.record(
        "session_create", owner=owner, client=client_ip(request),
        session=row["session_id"],
    )
    return await container.sessions.to_view(row)


@router.get("", response_model=list[SessionView])
async def list_sessions(
    request: Request,
    owner: str = Depends(require_auth),
    limit: int = 100,
    offset: int = 0,
):
    container = get_container(request)
    scope = None if owner == "anonymous" else owner
    rows = await container.sessions.list(owner=scope, limit=limit, offset=offset)
    return [await container.sessions.to_view(r) for r in rows]


@router.get("/{session_id}", response_model=SessionView)
async def get_session(
    session_id: str, request: Request, owner: str = Depends(require_auth)
):
    container = get_container(request)
    row = await container.sessions.get(session_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="session not found")
    return await container.sessions.to_view(row)


@router.get("/{session_id}/messages")
async def get_messages(
    session_id: str, request: Request, owner: str = Depends(require_auth),
    limit: int = 200,
):
    container = get_container(request)
    row = await container.sessions.get(session_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="session not found")
    msgs = await container.sessions.messages(session_id, limit=limit)
    return {"session_id": session_id, "messages": msgs}


@router.delete("/{session_id}")
async def delete_session(
    session_id: str, request: Request, owner: str = Depends(require_auth)
):
    container = get_container(request)
    row = await container.sessions.get(session_id)
    if row is None or not _owns(row, owner):
        raise HTTPException(status_code=404, detail="session not found")
    ok = await container.sessions.delete(session_id)
    container.audit.record(
        "session_delete", owner=owner, client=client_ip(request),
        session=session_id, outcome="ok" if ok else "not_found",
    )
    return {"deleted": ok, "session_id": session_id}

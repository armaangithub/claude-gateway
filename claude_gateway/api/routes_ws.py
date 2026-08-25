"""WebSocket endpoint: /ws/jobs/{job_id} — live job event stream."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..logging_config import get_logger
from ..security import AuthError
from .deps import get_container

router = APIRouter()
log = get_logger("gateway.ws")


@router.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str) -> None:
    container = websocket.app.state.container

    # Authenticate from query params or headers before accepting.
    token_q = websocket.query_params.get("api_key") or websocket.query_params.get("token")
    authz = websocket.headers.get("authorization")
    xkey = websocket.headers.get("x-api-key") or token_q
    try:
        owner = container.auth.authenticate(authz, xkey)
    except AuthError:
        await websocket.close(code=1008)
        return

    row = await container.db.get_job(job_id)
    if row is None or (owner != "anonymous" and row.get("owner") not in (owner, None)):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        channel = container.broker.get(job_id)
        if channel is not None:
            async for ev in channel.subscribe(replay=True):
                await websocket.send_json(ev)
        else:
            for e in await container.db.list_events(job_id):
                await websocket.send_json(
                    {"type": e["type"], "seq": e["seq"], "ts": e["ts"], "data": e["data"]}
                )
        await websocket.close()
    except WebSocketDisconnect:
        log.debug("ws client disconnected from job %s", job_id)
    except Exception as e:  # pragma: no cover
        log.warning("ws error for job %s: %s", job_id, e)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass

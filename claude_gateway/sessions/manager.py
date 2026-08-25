"""SessionManager — create / resume / delete / list sessions + persistence.

A session is the unit of conversation continuity. Its identity (a UUID) is used
directly as the Claude Code session id, so the on-disk transcript the CLI writes
*is* our persistence: sessions survive a server restart with no extra work. This
manager owns the metadata (workspace, owner, cost, timestamps) in SQLite and
delegates the live subprocess lifecycle to the backend's warm pool.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from ..backend.base import ClaudeBackend
from ..config import Settings
from ..logging_config import get_logger
from ..models import SessionView
from ..storage import Database

log = get_logger("gateway.sessions")


class SessionManager:
    def __init__(
        self, db: Database, settings: Settings, backend: ClaudeBackend
    ) -> None:
        self.db = db
        self.settings = settings
        self.backend = backend

    # ---- workspace --------------------------------------------------------
    def _workspace_for(self, session_id: str, override: str | None) -> str:
        if override:
            p = Path(override).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return str(p.resolve())
        p = self.settings.resolved_workspaces_dir() / session_id
        p.mkdir(parents=True, exist_ok=True)
        return str(p.resolve())

    # ---- create / ensure --------------------------------------------------
    async def create(
        self,
        *,
        kind: str = "chat",
        title: str | None = None,
        workspace: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        owner: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        sid = session_id or str(uuid.uuid4())
        ws = self._workspace_for(sid, workspace)
        now = time.time()
        row = {
            "session_id": sid,
            "claude_session_id": sid,
            "title": title,
            "kind": kind,
            "workspace": ws,
            "model": model,
            "system_prompt": system_prompt,
            "owner": owner,
            "created_at": now,
            "last_activity": now,
            "message_count": 0,
            "total_cost_usd": 0.0,
            "meta": {},
        }
        await self.db.upsert_session(row)
        log.info("created session %s (kind=%s ws=%s)", sid, kind, ws)
        return row

    async def ensure(
        self,
        session_id: str | None,
        *,
        kind: str,
        owner: str | None,
        workspace: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        title: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return (session_row, created). Creates one if session_id is None or
        unknown."""
        if session_id:
            existing = await self.db.get_session(session_id)
            if existing is not None:
                return existing, False
            # Caller supplied an id we don't know — create it with that id so
            # they can keep using it.
            row = await self.create(
                kind=kind, owner=owner, workspace=workspace, model=model,
                system_prompt=system_prompt, title=title, session_id=session_id,
            )
            return row, True
        row = await self.create(
            kind=kind, owner=owner, workspace=workspace, model=model,
            system_prompt=system_prompt, title=title,
        )
        return row, True

    # ---- read -------------------------------------------------------------
    async def get(self, session_id: str) -> dict[str, Any] | None:
        return await self.db.get_session(session_id)

    async def list(
        self, owner: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        return await self.db.list_sessions(owner=owner, limit=limit, offset=offset)

    def is_warm(self, session_id: str) -> bool:
        return session_id in set(getattr(self.backend, "warm_session_ids", lambda: [])())

    async def to_view(self, row: dict[str, Any]) -> SessionView:
        return SessionView(
            session_id=row["session_id"],
            title=row.get("title"),
            kind=row.get("kind", "chat"),
            workspace=row.get("workspace", ""),
            created_at=row.get("created_at", 0.0),
            last_activity=row.get("last_activity", 0.0),
            message_count=row.get("message_count", 0),
            total_cost_usd=row.get("total_cost_usd", 0.0),
            warm=self.is_warm(row["session_id"]),
        )

    async def messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return await self.db.list_messages(session_id, limit=limit)

    # ---- update -----------------------------------------------------------
    async def record_turn(
        self,
        session_id: str,
        *,
        cost: float = 0.0,
        claude_session_id: str | None = None,
    ) -> None:
        await self.db.touch_session(
            session_id,
            add_cost=cost,
            add_messages=2,  # one user + one assistant
            claude_session_id=claude_session_id,
        )

    # ---- delete -----------------------------------------------------------
    async def delete(self, session_id: str, *, purge_transcript: bool = True) -> bool:
        row = await self.db.get_session(session_id)
        if row is None:
            return False
        # 1) tear down warm client/subprocess
        try:
            await self.backend.close_session(session_id)
        except Exception as e:  # pragma: no cover
            log.warning("close_session failed for %s: %s", session_id, e)
        # 2) remove the on-disk Claude transcript (best-effort, SDK-only)
        if purge_transcript:
            try:
                import claude_agent_sdk as sdk  # noqa: PLC0415

                sdk.delete_session(
                    session_id, directory=row.get("workspace")
                )
            except Exception as e:
                log.debug("transcript purge skipped for %s: %s", session_id, e)
        # 3) drop metadata. Workspace files are intentionally preserved.
        await self.db.delete_session(session_id)
        log.info("deleted session %s", session_id)
        return True

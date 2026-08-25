"""Async SQLite storage.

Tables: sessions, jobs, events, messages, usage.

A single :class:`aiosqlite.Connection` is shared process-wide. aiosqlite runs
each connection on its own thread and serializes operations, so concurrent
``await`` calls are safe. WAL mode keeps readers from blocking the writer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from ..logging_config import get_logger

log = get_logger("gateway.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    claude_session_id TEXT,
    title             TEXT,
    kind              TEXT NOT NULL DEFAULT 'chat',
    workspace         TEXT NOT NULL,
    model             TEXT,
    system_prompt     TEXT,
    owner             TEXT,
    created_at        REAL NOT NULL,
    last_activity     REAL NOT NULL,
    message_count     INTEGER NOT NULL DEFAULT 0,
    total_cost_usd    REAL NOT NULL DEFAULT 0,
    meta              TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    session_id      TEXT,
    kind            TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    response        TEXT,
    error           TEXT,
    status          TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,
    duration_ms     INTEGER,
    num_turns       INTEGER,
    backend         TEXT,
    owner           TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    meta            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    type      TEXT NOT NULL,
    data      TEXT NOT NULL DEFAULT '{}',
    ts        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    job_id      TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT,
    session_id      TEXT,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    ts              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("database ready at %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected")
        return self._db

    # ---- sessions ---------------------------------------------------------
    async def upsert_session(self, row: dict[str, Any]) -> None:
        row = {**row}
        row.setdefault("created_at", time.time())
        row.setdefault("last_activity", time.time())
        row.setdefault("kind", "chat")
        row.setdefault("message_count", 0)
        row.setdefault("total_cost_usd", 0.0)
        row["meta"] = json.dumps(row.get("meta", {}))
        cols = (
            "session_id,claude_session_id,title,kind,workspace,model,system_prompt,"
            "owner,created_at,last_activity,message_count,total_cost_usd,meta"
        )
        placeholders = ",".join(["?"] * len(cols.split(",")))
        vals = [row.get(c) for c in cols.split(",")]
        await self.db.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET "
            f"claude_session_id=excluded.claude_session_id,"
            f"title=COALESCE(excluded.title, sessions.title),"
            f"last_activity=excluded.last_activity,"
            f"message_count=excluded.message_count,"
            f"total_cost_usd=excluded.total_cost_usd",
            vals,
        )
        await self.db.commit()

    async def touch_session(
        self, session_id: str, *, add_cost: float = 0.0, add_messages: int = 0,
        claude_session_id: str | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE sessions SET last_activity=?, total_cost_usd=total_cost_usd+?, "
            "message_count=message_count+?, "
            "claude_session_id=COALESCE(?, claude_session_id) WHERE session_id=?",
            (time.time(), add_cost, add_messages, claude_session_id, session_id),
        )
        await self.db.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        )
        r = await cur.fetchone()
        return _row(r)

    async def list_sessions(
        self, owner: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        if owner is not None:
            cur = await self.db.execute(
                "SELECT * FROM sessions WHERE owner=? ORDER BY last_activity DESC "
                "LIMIT ? OFFSET ?",
                (owner, limit, offset),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM sessions ORDER BY last_activity DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [_row(r) for r in await cur.fetchall()]

    async def delete_session(self, session_id: str) -> None:
        await self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        await self.db.commit()

    # ---- jobs -------------------------------------------------------------
    # Integer columns declared NOT NULL DEFAULT 0 — an explicit NULL would
    # violate the constraint, so we fill them when the caller omits them.
    _JOB_NUMERIC_DEFAULTS = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
    }

    async def insert_job(self, row: dict[str, Any]) -> None:
        row = {**row}
        row.setdefault("created_at", time.time())
        row["meta"] = json.dumps(row.get("meta", {}))
        cols = (
            "job_id,session_id,kind,prompt,response,error,status,input_tokens,"
            "output_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,"
            "cost_usd,duration_ms,num_turns,backend,owner,created_at,started_at,"
            "finished_at,meta"
        ).split(",")
        placeholders = ",".join(["?"] * len(cols))
        await self.db.execute(
            f"INSERT INTO jobs ({','.join(cols)}) VALUES ({placeholders})",
            [row.get(c, self._JOB_NUMERIC_DEFAULTS.get(c)) for c in cols],
        )
        await self.db.commit()

    async def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "meta" in fields and not isinstance(fields["meta"], str):
            fields["meta"] = json.dumps(fields["meta"])
        sets = ",".join(f"{k}=?" for k in fields)
        await self.db.execute(
            f"UPDATE jobs SET {sets} WHERE job_id=?", [*fields.values(), job_id]
        )
        await self.db.commit()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return _row(await cur.fetchone())

    async def list_jobs(
        self,
        session_id: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if session_id:
            clauses.append("session_id=?"); params.append(session_id)
        if status:
            clauses.append("status=?"); params.append(status)
        if owner is not None:
            clauses.append("owner=?"); params.append(owner)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params += [limit, offset]
        cur = await self.db.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [_row(r) for r in await cur.fetchall()]

    # ---- events -----------------------------------------------------------
    async def insert_event(
        self, job_id: str, seq: int, type_: str, data: dict[str, Any]
    ) -> None:
        await self.db.execute(
            "INSERT INTO events (job_id, seq, type, data, ts) VALUES (?,?,?,?,?)",
            (job_id, seq, type_, json.dumps(data, default=str), time.time()),
        )
        await self.db.commit()

    async def list_events(self, job_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,)
        )
        out = []
        for r in await cur.fetchall():
            d = _row(r)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out

    # ---- messages ---------------------------------------------------------
    async def insert_message(
        self, session_id: str | None, job_id: str | None, role: str, content: str
    ) -> None:
        await self.db.execute(
            "INSERT INTO messages (session_id, job_id, role, content, ts) "
            "VALUES (?,?,?,?,?)",
            (session_id, job_id, role, content, time.time()),
        )
        await self.db.commit()

    async def list_messages(
        self, session_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY ts LIMIT ?",
            (session_id, limit),
        )
        return [_row(r) for r in await cur.fetchall()]

    # ---- usage ------------------------------------------------------------
    async def insert_usage(self, row: dict[str, Any]) -> None:
        row = {**row}
        row.setdefault("ts", time.time())
        cols = (
            "job_id,session_id,model,input_tokens,output_tokens,cache_read_tokens,"
            "cache_creation_tokens,total_tokens,cost_usd,ts"
        ).split(",")
        placeholders = ",".join(["?"] * len(cols))
        await self.db.execute(
            f"INSERT INTO usage ({','.join(cols)}) VALUES ({placeholders})",
            [row.get(c, 0) for c in cols],
        )
        await self.db.commit()

    async def usage_totals(self) -> dict[str, Any]:
        cur = await self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tokens, "
            "COALESCE(SUM(cost_usd),0) cost FROM usage"
        )
        r = await cur.fetchone()
        return {"records": r["n"], "total_tokens": r["tokens"], "total_cost_usd": r["cost"]}

    async def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        q = f"SELECT COUNT(*) n FROM {table}"  # table is internal-only, never user input
        if where:
            q += f" WHERE {where}"
        cur = await self.db.execute(q, params)
        r = await cur.fetchone()
        return int(r["n"])


def _row(r: aiosqlite.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    if "meta" in d and isinstance(d["meta"], str):
        try:
            d["meta"] = json.loads(d["meta"])
        except (ValueError, TypeError):
            d["meta"] = {}
    return d

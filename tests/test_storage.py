"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import pytest

from claude_gateway.storage import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


async def test_session_crud(db):
    await db.upsert_session(
        {"session_id": "s1", "kind": "chat", "workspace": "/tmp/s1", "owner": "o1"}
    )
    row = await db.get_session("s1")
    assert row is not None and row["kind"] == "chat" and row["owner"] == "o1"

    await db.touch_session("s1", add_cost=0.5, add_messages=2, claude_session_id="c1")
    row = await db.get_session("s1")
    assert row["total_cost_usd"] == 0.5
    assert row["message_count"] == 2
    assert row["claude_session_id"] == "c1"

    rows = await db.list_sessions(owner="o1")
    assert len(rows) == 1
    await db.delete_session("s1")
    assert await db.get_session("s1") is None


async def test_job_lifecycle(db):
    await db.insert_job(
        {"job_id": "j1", "session_id": "s1", "kind": "chat", "prompt": "hi",
         "status": "QUEUED", "owner": "o1"}
    )
    await db.update_job("j1", status="RUNNING")
    await db.update_job(
        "j1", status="COMPLETED", response="hello", total_tokens=42, cost_usd=0.01
    )
    row = await db.get_job("j1")
    assert row["status"] == "COMPLETED"
    assert row["response"] == "hello"
    assert row["total_tokens"] == 42

    jobs = await db.list_jobs(session_id="s1")
    assert len(jobs) == 1
    completed = await db.list_jobs(status="COMPLETED")
    assert len(completed) == 1


async def test_events_messages_usage(db):
    await db.insert_event("j1", 1, "job.started", {"x": 1})
    await db.insert_event("j1", 2, "job.completed", {"ok": True})
    events = await db.list_events("j1")
    assert [e["seq"] for e in events] == [1, 2]
    assert events[1]["data"] == {"ok": True}

    await db.insert_message("s1", "j1", "user", "hi")
    await db.insert_message("s1", "j1", "assistant", "yo")
    msgs = await db.list_messages("s1")
    assert len(msgs) == 2 and msgs[0]["role"] == "user"

    await db.insert_usage(
        {"job_id": "j1", "session_id": "s1", "model": "m", "total_tokens": 100,
         "cost_usd": 0.05}
    )
    totals = await db.usage_totals()
    assert totals["total_tokens"] == 100
    assert totals["total_cost_usd"] == 0.05

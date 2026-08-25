"""Unit tests for the streaming event broker."""

from __future__ import annotations

import asyncio

import pytest

from claude_gateway.streaming import EventBroker


async def test_replay_then_live():
    broker = EventBroker()
    broker.publish("j1", {"type": "job.started", "seq": 1})
    broker.publish("j1", {"type": "job.message", "seq": 2})

    channel = broker.get("j1")
    received = []

    async def consume():
        async for ev in channel.subscribe(replay=True):
            received.append(ev["seq"])

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)  # let it replay the buffer
    broker.publish("j1", {"type": "job.completed", "seq": 3})
    broker.close("j1")
    await asyncio.wait_for(task, timeout=2)
    assert received == [1, 2, 3]


async def test_late_subscriber_gets_full_replay():
    broker = EventBroker()
    for i in range(1, 4):
        broker.publish("j2", {"type": "x", "seq": i})
    broker.close("j2")

    received = []
    async for ev in broker.get("j2").subscribe(replay=True):
        received.append(ev["seq"])
    assert received == [1, 2, 3]


async def test_multiple_subscribers():
    broker = EventBroker()
    ch = broker.get_or_create("j3")

    async def consume():
        out = []
        async for ev in ch.subscribe(replay=True):
            out.append(ev["seq"])
        return out

    t1 = asyncio.create_task(consume())
    t2 = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    ch.publish({"type": "x", "seq": 1})
    ch.close()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == [1] and r2 == [1]

"""Integration tests for the HTTP API (REST + SSE + WebSocket) via FakeBackend."""

from __future__ import annotations

import json


# ---- meta -----------------------------------------------------------------
def test_health(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "fake"
    assert body["backend_available"] is True


def test_models(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert any("opus" in i for i in ids)


# ---- chat -----------------------------------------------------------------
def test_chat_basic(client):
    r = client.post("/v1/chat", json={"prompt": "Explain this code"})
    assert r.status_code == 200
    body = r.json()
    assert body["response"].startswith("echo:")
    assert body["session_id"]
    assert body["usage"]["total_tokens"] == 20
    assert body["cost_usd"] == 0.0021


def test_chat_session_continuity(client):
    r1 = client.post("/v1/chat", json={"prompt": "first"})
    sid = r1.json()["session_id"]
    r2 = client.post("/v1/chat", json={"prompt": "second", "session_id": sid})
    assert r2.json()["session_id"] == sid
    # Session should now have 4 messages (2 turns).
    s = client.get(f"/v1/sessions/{sid}").json()
    assert s["message_count"] == 4
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert len(msgs) == 4


# ---- agent / code ---------------------------------------------------------
def test_agent_tool_calls(client):
    r = client.post("/v1/agent", json={"prompt": "do work"})
    assert r.status_code == 200
    body = r.json()
    assert body["tool_calls"], "expected tool calls"
    assert body["tool_calls"][0]["name"] == "Read"


def test_code_endpoint(client):
    r = client.post("/v1/code", json={"prompt": "fix the bug"})
    assert r.status_code == 200
    assert r.json()["tool_calls"][0]["name"] == "Read"


# ---- review / judge -------------------------------------------------------
def test_review_inline_diff(client):
    r = client.post("/v1/review", json={"diff": "- a\n+ b", "focus": ["bugs"]})
    assert r.status_code == 200
    assert r.json()["response"]


def test_judge_structured(client):
    r = client.post(
        "/v1/judge",
        json={
            "candidate": "The sky is blue.",
            "evidence": ["Rayleigh scattering", "observed daily"],
            "question": "Is the statement correct?",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "accept"
    assert body["confidence"] == 0.91
    assert body["key_factors"] == ["a", "b"]
    assert "job_id" in body


def test_judge_is_stateless(client, fake_backend):
    client.post("/v1/judge", json={"candidate": "x", "evidence": []})
    assert fake_backend.calls[-1]["stateless"] is True
    assert fake_backend.calls[-1]["output_format"] is not None


# ---- sessions -------------------------------------------------------------
def test_session_crud(client):
    r = client.post("/v1/sessions", json={"kind": "agent", "title": "t"})
    sid = r.json()["session_id"]
    assert r.json()["kind"] == "agent"

    assert any(s["session_id"] == sid for s in client.get("/v1/sessions").json())
    assert client.get(f"/v1/sessions/{sid}").status_code == 200

    d = client.delete(f"/v1/sessions/{sid}")
    assert d.json()["deleted"] is True
    assert client.get(f"/v1/sessions/{sid}").status_code == 404


# ---- jobs -----------------------------------------------------------------
def test_jobs_list_and_get(client):
    r = client.post("/v1/chat", json={"prompt": "hello"})
    jid = r.json()["job_id"]
    jobs = client.get("/v1/jobs").json()
    assert any(j["job_id"] == jid for j in jobs)
    one = client.get(f"/v1/jobs/{jid}").json()
    assert one["status"] == "COMPLETED"
    events = client.get(f"/v1/jobs/{jid}/events").json()["events"]
    types = [e["type"] for e in events]
    assert "job.started" in types and "job.completed" in types


# ---- streaming SSE --------------------------------------------------------
def test_sse_stream_replay(client):
    r = client.post("/v1/chat", json={"prompt": "stream me"})
    jid = r.json()["job_id"]
    sse = client.get(f"/v1/stream/{jid}")
    assert sse.status_code == 200
    text = sse.text
    assert "event: job.started" in text
    assert "event: job.completed" in text


def test_post_stream_true(client):
    r = client.post("/v1/chat", json={"prompt": "live", "stream": True})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "X-Job-Id" in r.headers
    assert "event: job.completed" in r.text


# ---- websocket ------------------------------------------------------------
def test_websocket_stream(client):
    r = client.post("/v1/chat", json={"prompt": "ws"})
    jid = r.json()["job_id"]
    with client.websocket_connect(f"/ws/jobs/{jid}") as ws:
        seen = []
        try:
            while True:
                seen.append(ws.receive_json())
        except Exception:
            pass
    types = [e["type"] for e in seen]
    assert "job.completed" in types


# ---- auth -----------------------------------------------------------------
def test_auth_required(client_factory):
    c, _ = client_factory(API_KEY="topsecret")
    assert c.post("/v1/chat", json={"prompt": "x"}).status_code == 401
    ok = c.post("/v1/chat", json={"prompt": "x"},
                headers={"Authorization": "Bearer topsecret"})
    assert ok.status_code == 200
    # health is public.
    assert c.get("/v1/health").status_code == 200


def test_rate_limit(client_factory):
    c, _ = client_factory(RATE_LIMIT_PER_MIN=60, RATE_LIMIT_BURST=2)
    codes = [c.post("/v1/chat", json={"prompt": "x"}).status_code for _ in range(5)]
    assert 429 in codes

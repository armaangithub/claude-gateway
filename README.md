# Local Claude Code Gateway

Expose your locally-installed **Claude Code** (via the **Claude Agent SDK**) as a
localhost HTTP API — REST, Server-Sent-Events streaming, and WebSocket — so
**SAMURAI** and any other local application can talk to Claude Code over HTTP
while preserving **sessions**, **streaming output**, and **agent (tool)
behavior**.

```
Applications ──HTTP──▶ Claude Gateway ──▶ Claude Agent SDK ──▶ Claude Code Runtime
```

* No process-per-request: a long-running `claude` subprocess is kept **warm per
  session** and reused across turns. Idle sessions are reaped after a TTL.
* Sessions are persisted by the Claude Code runtime to disk, so they **survive a
  gateway restart** — verified: set a codeword, restart the server, ask for it
  back, get it.
* Uses your existing Claude subscription auth (macOS Keychain) on the host — **no
  API key required** for bare-metal runs.

---

## Why the SDK (and the test-first approach)

This was built SDK-first and proven before scaling. The Claude Agent SDK
(`claude-agent-sdk`) drives a `claude` subprocess in stream-json mode; it gives
us native session resume (`resume=<id>`), structured output, partial-message
streaming, and rich `ResultMessage` usage/cost data. A `claude -p` CLI fallback
exists for when the SDK can't import.

A `smoke_test.py` validates the four foundations against your real environment
before any of the gateway matters:

```bash
.venv/bin/python smoke_test.py   # Claude responds · session persists · streaming · tool events
```

---

## Quickstart

```bash
# 1. Create a venv and install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .            # installs the `gateway` CLI into the venv

# 2. Activate the venv so the `gateway` command is on your PATH
source .venv/bin/activate             # (Windows: .venv\Scripts\activate)
#   …or skip activation and call the binary directly: .venv/bin/gateway <cmd>

# 3. Configure (optional — defaults are localhost-safe)
cp .env.example .env                  # then edit API_KEY (default: changeme)

# 4. Start  (8080 is often taken — e.g. Burp Suite — so pick a free port)
PORT=8765 gateway start               # background; or add --foreground
#   ✓ gateway up at http://127.0.0.1:8765  (backend=sdk, sdk=0.2.93)
#     dashboard: http://127.0.0.1:8765/dashboard
#     docs:      http://127.0.0.1:8765/docs

# 5. Use it
curl -s http://127.0.0.1:8765/v1/health -H "Authorization: Bearer changeme"
```

> **`gateway: command not found`?** You didn't activate the venv. Either run
> `source .venv/bin/activate` first, or call `.venv/bin/gateway <cmd>` directly.
>
> **Won't bind / health returns someone else's page?** Another service owns the
> port (8080 is commonly Burp Suite). Start with `PORT=8765 gateway start`, or
> set `PORT=8765` in `.env`.

---

## REST API

All `/v1/*` endpoints require `Authorization: Bearer <API_KEY>` (or
`X-API-Key: <key>`) unless `API_KEY` is empty. `/v1/health`, `/metrics`, and
`/dashboard` are public.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat` | Conversational turn (no tools, lean + isolated). |
| POST | `/v1/agent` | Agentic run with the full Claude Code toolset in a workspace. |
| POST | `/v1/code` | Like `/v1/agent`, tuned for software work. |
| POST | `/v1/review` | Code review of a diff, code blob, or files (read-only). |
| POST | `/v1/judge` | **SAMURAI Final Judge** — structured verdict + confidence. |
| GET | `/v1/models` | List available models. |
| GET | `/v1/health` | Backend/SDK status, counts, uptime. |
| GET | `/v1/sessions` · `POST` · `GET/{id}` · `DELETE/{id}` | Session CRUD. |
| GET | `/v1/sessions/{id}/messages` | Conversation transcript. |
| GET | `/v1/jobs` · `GET/{id}` · `POST/{id}/cancel` · `GET/{id}/events` | Job inspection. |
| GET | `/v1/stream/{job_id}` | **SSE** event stream for a job. |
| WS | `/ws/jobs/{job_id}` | **WebSocket** event stream for a job. |
| GET | `/v1/metrics` · `/metrics` | JSON metrics · Prometheus exposition. |
| GET | `/dashboard` · `/docs` | Live dashboard · OpenAPI UI. |

### `POST /v1/chat`

```bash
curl -s http://127.0.0.1:8080/v1/chat \
  -H "Authorization: Bearer changeme" -H "Content-Type: application/json" \
  -d '{"prompt":"Explain this code","model":"sonnet"}'
```
```json
{
  "job_id": "3dbb6dfb-…",
  "session_id": "59552af3-…",
  "response": "…",
  "usage": {"input_tokens": 10, "output_tokens": 90, "total_tokens": 7426, "...": "..."},
  "cost_usd": 0.0096, "duration_ms": 2313, "num_turns": 1,
  "model": "claude-…", "status": "COMPLETED", "backend": "sdk"
}
```
Pass the returned `session_id` back on the next call to continue the conversation.

### `POST /v1/agent` / `POST /v1/code`

Runs the real Claude Code agent (tools enabled, `bypassPermissions` by default —
there is no human at the HTTP end) inside a managed per-session workspace.

```bash
curl -s http://127.0.0.1:8080/v1/agent \
  -H "Authorization: Bearer changeme" -H "Content-Type: application/json" \
  -d '{"prompt":"Create gw_proof.txt with TOOLS_WORK, then read it back.",
       "permission_mode":"bypassPermissions"}'
```
The response includes a `tool_calls` array (`name`, `arguments`, `result_summary`,
`execution_ms`, `is_error`). Provide `"workspace": "/abs/path"` to run against
your own project directory (project-local `CLAUDE.md` is honored).

### `POST /v1/judge` — SAMURAI Final Judge

```bash
curl -s http://127.0.0.1:8080/v1/judge \
  -H "Authorization: Bearer changeme" -H "Content-Type: application/json" \
  -d '{"candidate":"The Earth orbits the Sun.",
       "evidence":["Heliocentric model","Stellar parallax"],
       "question":"Is the candidate correct?"}'
```
```json
{
  "job_id": "a113…",
  "verdict": "accept",
  "confidence": 0.99,
  "reasoning": "…directly supported by both pieces of evidence…",
  "key_factors": ["Heliocentric model directly supports the claim", "…"],
  "usage": {"...": "..."}, "cost_usd": 0.0578
}
```
Judging is **stateless and isolated** (`setting_sources=[]`, fresh context per
call) so verdicts never bleed across requests, and uses the SDK's
**structured-output** mode (with JSON-parsing fallbacks) to guarantee the shape.

**SAMURAI integration:** point SAMURAI's Final Judge at
`http://127.0.0.1:8080/v1/judge` instead of calling Claude directly.

---

## Streaming

Two ways to get live, Claude-Code-style output:

**1. Inline SSE** — add `"stream": true` to any `POST`:
```bash
curl -N http://127.0.0.1:8080/v1/chat \
  -H "Authorization: Bearer changeme" -H "Content-Type: application/json" \
  -d '{"prompt":"Count to 5","stream":true}'
```
**2. Out-of-band** — submit a job, then attach to `GET /v1/stream/{job_id}` (SSE)
or `WS /ws/jobs/{job_id}`. The streaming `POST` returns `X-Job-Id` /
`X-Session-Id` headers.

Event frames:
```
event: job.started     data: {"job_id":"…","session_id":"…","kind":"chat"}
event: job.thinking    data: {"text":"…model reasoning…"}
event: job.message     data: {"text":"alpha beta gamma","delta":true}
event: job.tool_call   data: {"name":"Read","arguments":{...},"tool_use_id":"…"}
event: job.tool_result data: {"name":"Read","result_summary":"…","execution_ms":119}
event: job.completed   data: {"response":"…","usage":{...},"cost_usd":…}
event: job.failed      data: {"error":"…"}
```
The broker buffers events, so a subscriber that attaches late still replays the
whole stream; finished jobs replay from the database.

---

## Sessions

A session is the unit of conversation continuity. Its id **is** the Claude Code
session UUID, so the on-disk transcript is the persistence — sessions survive
restarts with no extra work.

* Omit `session_id` to create one (returned in the response).
* Pass it back to continue. A warm subprocess is reused; after a restart the
  session is transparently resumed from disk (`resume=<id>`).
* `DELETE /v1/sessions/{id}` tears down the warm client, purges the transcript,
  and removes metadata (workspace files are preserved).

---

## Security

* **API-key auth** — shared bearer secret (`API_KEY`); empty disables it (dev only).
* **Rate limiting** — token bucket per identity (`RATE_LIMIT_PER_MIN`, `_BURST`).
* **Request + audit logging** — operational request logs plus an append-only
  JSONL audit trail (`~/.claude_gateway/audit.log`) of submissions/deletes/auth
  failures.
* **Session isolation** — per-session workspace directories; sessions/jobs are
  scoped to the owning key.
* **Localhost-only mode** — refuses non-loopback clients unless `ALLOW_REMOTE=1`;
  `main` also refuses to bind a non-loopback address without it.
* **CORS** — configurable via `CORS_ORIGINS`.

---

## Configuration (environment variables)

| Var | Default | Notes |
|---|---|---|
| `CLAUDE_BACKEND` | `auto` | `sdk` \| `cli` \| `auto` (prefer SDK, fall back to CLI). |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | Bind address. |
| `API_KEY` | `changeme` | Bearer secret; empty disables auth. |
| `DEFAULT_MODEL` | _(SDK default)_ | e.g. `sonnet`, `opus`, `haiku`. |
| `DEFAULT_PERMISSION_MODE` | `bypassPermissions` | For agent/code endpoints. |
| `MAX_TURNS` / `MAX_BUDGET_USD` | `40` / _(none)_ | Per-run bounds. |
| `JOB_TIMEOUT_S` | `600` | Hard per-job timeout. |
| `SESSION_IDLE_TTL_S` | `900` | Reap warm subprocesses after idle. |
| `RATE_LIMIT_PER_MIN` / `_BURST` | `120` / `30` | `0` disables. |
| `LOCALHOST_ONLY` / `ALLOW_REMOTE` | `1` / `0` | Loopback enforcement. |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `0` | Structured logs. |
| `OTEL_ENABLED` / `OTEL_ENDPOINT` | `0` / _(none)_ | OpenTelemetry. |
| `GATEWAY_HOME` | `~/.claude_gateway` | DB, workspaces, logs, pidfile. |

See `.env.example`.

---

## CLI

```bash
gateway start [--foreground]   # start (background by default)
gateway stop                   # stop
gateway status                 # pid + /v1/health
gateway sessions [--limit N]   # list sessions
gateway jobs [--limit N] [--status RUNNING]
```
Authenticated commands read `--api-key`, then `$API_KEY`, then config.

---

## Dashboard & Observability

* **`/dashboard`** — live view of jobs, sessions, cost, tokens, latency, backend
  health (enter the API key once; stored in `localStorage`). Auto-refreshes.
* **`/v1/metrics`** (JSON) and **`/metrics`** (Prometheus): request counts,
  latency histograms, token + cost totals, error counts, active/warm sessions,
  running jobs.
* **OpenTelemetry** traces + metrics when `OTEL_ENABLED=1` (degrades gracefully
  if the OTLP exporter or packages are absent).

---

## Docker

```bash
API_KEY=secret docker compose up --build
```
> **Auth in containers:** the host's macOS-Keychain subscription auth is **not**
> available inside a container. Provide `ANTHROPIC_API_KEY` for Docker runs. On
> bare metal (`gateway start`), subscription auth works with no key.

---

## Testing

```bash
.venv/bin/python -m pytest tests -q        # 28 unit + integration tests (FakeBackend, no API calls)
.venv/bin/python smoke_test.py             # real SDK end-to-end (costs a few cents)
```
Unit/integration tests inject a `FakeBackend`, so they're fast, deterministic,
and free. The smoke test and the curl examples above exercise the real SDK.

---

## Project structure

```
gateway/
├── claude_gateway/
│   ├── app.py            # FastAPI factory, middleware, CORS, lifespan
│   ├── main.py           # uvicorn entrypoint
│   ├── container.py      # wires all components; model catalog
│   ├── config.py         # env-driven settings
│   ├── models.py         # Pydantic request/response schemas + enums
│   ├── observability.py  # metrics registry + optional OpenTelemetry
│   ├── api/              # routes: chat, review/judge, sessions, jobs, ws, meta
│   ├── backend/          # ClaudeBackend interface + SDK & CLI backends + events
│   ├── sessions/         # SessionManager (create/resume/delete/list + persistence)
│   ├── jobs/             # JobManager (states, event pipeline, cancellation)
│   ├── storage/          # SQLite (sessions, jobs, events, messages, usage)
│   ├── streaming/        # pub/sub broker for SSE + WebSocket
│   ├── security/         # auth, rate limiting, audit
│   ├── dashboard/        # single-page UI
│   └── cli/              # gateway start|stop|status|sessions|jobs
├── tests/                # unit + integration (FakeBackend)
├── smoke_test.py         # real-SDK foundation check
├── Dockerfile · docker-compose.yml · pyproject.toml · requirements.txt · .env.example
```

## Architecture notes

* **Backend interface** (`ClaudeBackend`) with two implementations
  (`ClaudeSDKBackend`, `ClaudeCLIBackend`); `build_backend` chooses at startup.
* **Warm pool**: `ClaudeSDKBackend` keeps one `ClaudeSDKClient` per session, with
  a per-session lock (turns serialize) and an idle reaper. Stateless endpoints
  (`/judge`) use one-shot `query()` for isolation.
* **Jobs**: every request is a `Job` (`QUEUED→RUNNING→COMPLETED|FAILED|CANCELLED`)
  run as a background task that normalizes backend events, persists
  events/messages/usage, fans out to the broker, and resolves a future for the
  synchronous REST path.
* The SDK raises on error results (e.g. max-turns/budget); these are caught and
  surfaced as `FAILED` jobs (HTTP 502), never crashes.

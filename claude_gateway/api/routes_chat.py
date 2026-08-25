"""Conversational + agentic endpoints: /v1/chat, /v1/agent, /v1/code."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..backend.base import RunConfig
from ..container import Container
from ..models import (
    AgentRequest,
    AgentResponse,
    ChatRequest,
    ChatResponse,
    CodeRequest,
    ErrorResponse,
    JobState,
    ToolCallView,
    Usage,
)
from .deps import client_ip, get_container, require_auth
from .runner import stream_job_sse, submit_and_wait, submit_job

router = APIRouter(tags=["chat"])

DEFAULT_CHAT_PROMPT = (
    "You are Claude, a helpful assistant accessed over a local HTTP gateway. "
    "Answer directly and concisely."
)
CODE_APPEND_PROMPT = (
    "Focus on software engineering: read before you edit, prefer minimal, correct "
    "changes, and verify your work."
)


def _usage(result: dict[str, Any]) -> Usage:
    return Usage.from_sdk(result.get("usage"))


def _error_response(result: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content=ErrorResponse(
            error=result.get("error", "execution failed"),
            job_id=result.get("job_id"),
            detail=result.get("status"),
        ).model_dump(),
    )


def _chat_response(result: dict[str, Any], backend: str) -> ChatResponse:
    return ChatResponse(
        job_id=result["job_id"],
        session_id=result.get("session_id"),
        response=result.get("response", ""),
        usage=_usage(result),
        cost_usd=result.get("cost_usd"),
        duration_ms=result.get("duration_ms"),
        num_turns=result.get("num_turns"),
        model=result.get("model"),
        status=JobState.COMPLETED,
        backend=backend,
    )


def _agent_response(result: dict[str, Any], backend: str) -> AgentResponse:
    tool_calls = [
        ToolCallView(
            name=tc.get("name") or "unknown",
            arguments=tc.get("arguments", {}),
            result_summary=tc.get("result_summary"),
            execution_ms=tc.get("execution_ms"),
            is_error=bool(tc.get("is_error")),
        )
        for tc in result.get("tool_calls", [])
    ]
    base = _chat_response(result, backend)
    return AgentResponse(**base.model_dump(), tool_calls=tool_calls)


async def _run(
    container: Container,
    owner: str,
    *,
    kind: str,
    prompt: str,
    config: RunConfig,
    session_id: str,
    resume: bool,
    stream: bool,
    client: str,
    agent_shape: bool,
):
    container.audit.record(
        "job_submit", owner=owner, client=client, kind=kind, session=session_id
    )
    if stream:
        handle = await submit_job(
            container, kind=kind, prompt=prompt, config=config,
            session_id=session_id, resume=resume, owner=owner,
        )
        return StreamingResponse(
            stream_job_sse(container, handle.job_id),
            media_type="text/event-stream",
            headers={
                "X-Job-Id": handle.job_id,
                "X-Session-Id": session_id,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    result = await submit_and_wait(
        container, kind=kind, prompt=prompt, config=config,
        session_id=session_id, resume=resume, owner=owner,
    )
    if result.get("status") != JobState.COMPLETED.value:
        return _error_response(result)
    return _agent_response(result, container.backend.name) if agent_shape else \
        _chat_response(result, container.backend.name)


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    session_row, created = await container.sessions.ensure(
        req.session_id, kind="chat", owner=owner, model=req.model,
        system_prompt=req.system_prompt,
    )
    sid = session_row["session_id"]
    config = RunConfig(
        model=req.model,  # None -> backend falls back to DEFAULT_MODEL (Opus 4.8)
        system_prompt=req.system_prompt or DEFAULT_CHAT_PROMPT,
        use_claude_code_preset=False,
        tools=[],
        max_turns=req.max_turns or container.settings.effective_max_turns(),
        effort=req.effort,
        stream=req.stream,
        cwd=session_row["workspace"],
        setting_sources=[],  # isolate: don't load the host's personal CLAUDE.md
    )
    resume = (not created) and not container.sessions.is_warm(sid)
    return await _run(
        container, owner, kind="chat", prompt=req.prompt, config=config,
        session_id=sid, resume=resume, stream=req.stream,
        client=client_ip(request), agent_shape=False,
    )


def _agent_config(req: AgentRequest, session_row: dict, container: Container,
                  append: str | None = None) -> RunConfig:
    s = container.settings
    workspace = req.workspace or session_row["workspace"]
    add_dirs = [workspace] if req.workspace else []
    return RunConfig(
        model=req.model,  # None -> backend falls back to DEFAULT_MODEL (Opus 4.8)
        use_claude_code_preset=True,
        append_system_prompt="\n\n".join(p for p in (append, req.system_prompt) if p) or None,
        tools=req.allowed_tools if req.allowed_tools else None,
        disallowed_tools=req.disallowed_tools or [],
        permission_mode=req.permission_mode or s.default_permission_mode,
        # No turn/budget cap by default — bounded only by the Claude Code plan.
        max_turns=req.max_turns or s.effective_max_turns(),
        max_budget_usd=req.max_budget_usd if req.max_budget_usd is not None else s.max_budget_usd,
        effort=req.effort,
        stream=req.stream,
        cwd=workspace,
        add_dirs=add_dirs,
        # Full local Claude Code power: load all settings (your skills, subagents,
        # CLAUDE.md) and enable every skill unless the caller narrows it.
        setting_sources=req.setting_sources,
        skills=req.skills if req.skills is not None else "all",
    )


@router.post("/v1/agent", response_model=AgentResponse)
async def agent(req: AgentRequest, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    session_row, created = await container.sessions.ensure(
        req.session_id, kind="agent", owner=owner, workspace=req.workspace,
        model=req.model, system_prompt=req.system_prompt,
    )
    sid = session_row["session_id"]
    config = _agent_config(req, session_row, container)
    resume = (not created) and not container.sessions.is_warm(sid)
    return await _run(
        container, owner, kind="agent", prompt=req.prompt, config=config,
        session_id=sid, resume=resume, stream=req.stream,
        client=client_ip(request), agent_shape=True,
    )


@router.post("/v1/code", response_model=AgentResponse)
async def code(req: CodeRequest, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    session_row, created = await container.sessions.ensure(
        req.session_id, kind="code", owner=owner, workspace=req.workspace,
        model=req.model, system_prompt=req.system_prompt,
    )
    sid = session_row["session_id"]
    config = _agent_config(req, session_row, container, append=CODE_APPEND_PROMPT)
    resume = (not created) and not container.sessions.is_warm(sid)
    return await _run(
        container, owner, kind="code", prompt=req.prompt, config=config,
        session_id=sid, resume=resume, stream=req.stream,
        client=client_ip(request), agent_shape=True,
    )

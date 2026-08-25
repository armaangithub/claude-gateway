"""Review + judge endpoints: /v1/review and /v1/judge (SAMURAI Final Judge)."""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..backend.base import RunConfig
from ..models import (
    ChatResponse,
    ErrorResponse,
    JobState,
    JudgeRequest,
    JudgeResponse,
    ReviewRequest,
    Usage,
)
from .deps import client_ip, get_container, require_auth
from .runner import stream_job_sse, submit_and_wait, submit_job

router = APIRouter(tags=["review"])

REVIEW_SYSTEM = (
    "You are a senior staff software engineer performing a rigorous code review. "
    "Identify correctness bugs, security vulnerabilities, and concrete, "
    "actionable improvements. Cite file:line where possible and order findings "
    "by severity. Be precise; do not invent issues."
)

JUDGE_SYSTEM = (
    "You are an impartial, rigorous expert judge. You evaluate a candidate answer "
    "against the provided evidence and criteria, then return a calibrated verdict. "
    "Reason from the evidence only; do not assume facts not present. Your "
    "confidence must reflect genuine certainty."
)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "description": "Concise verdict, e.g. 'accept', 'reject', or a short ruling.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the verdict, between 0 and 1.",
            },
            "reasoning": {"type": "string"},
            "key_factors": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "confidence", "reasoning"],
        "additionalProperties": False,
    },
}


def _build_review_prompt(req: ReviewRequest) -> str:
    parts: list[str] = []
    if req.prompt:
        parts.append(req.prompt.strip())
    if req.focus:
        parts.append("Focus areas: " + ", ".join(req.focus))
    if req.diff:
        parts.append("Review this unified diff:\n\n```diff\n" + req.diff + "\n```")
    if req.code:
        parts.append("Review this code:\n\n```\n" + req.code + "\n```")
    if req.files:
        parts.append(
            "Read and review these files using your tools, then report findings:\n"
            + "\n".join(f"- {f}" for f in req.files)
        )
    if not parts:
        parts.append("No content was provided to review.")
    return "\n\n".join(parts)


@router.post("/v1/review", response_model=ChatResponse)
async def review(req: ReviewRequest, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    prompt = _build_review_prompt(req)
    has_files = bool(req.files)

    # Files require read-only tools + directory access; inline diff/code does not.
    if has_files:
        add_dirs = sorted({os.path.dirname(os.path.abspath(f)) for f in req.files or []})
        config = RunConfig(
            model=req.model,
            system_prompt=REVIEW_SYSTEM,
            use_claude_code_preset=False,
            tools=["Read", "Grep", "Glob"],
            allowed_tools=["Read", "Grep", "Glob"],
            permission_mode="bypassPermissions",
            max_turns=container.settings.effective_max_turns(),
            cwd=add_dirs[0] if add_dirs else None,
            add_dirs=add_dirs,
            stream=req.stream,
            setting_sources=["project"],
        )
    else:
        config = RunConfig(
            model=req.model,
            system_prompt=REVIEW_SYSTEM,
            use_claude_code_preset=False,
            tools=[],
            max_turns=2,
            stream=req.stream,
            setting_sources=[],
        )

    container.audit.record(
        "job_submit", owner=owner, client=client_ip(request), kind="review"
    )
    # Reviews run stateless (each independent) unless a session is supplied.
    stateless = req.session_id is None
    session_id = req.session_id
    resume = False
    if not stateless:
        row, created = await container.sessions.ensure(
            req.session_id, kind="review", owner=owner
        )
        session_id = row["session_id"]
        resume = (not created) and not container.sessions.is_warm(session_id)
        if not config.cwd:
            config.cwd = row["workspace"]

    if req.stream:
        handle = await submit_job(
            container, kind="review", prompt=prompt, config=config,
            session_id=session_id, resume=resume, stateless=stateless, owner=owner,
        )
        return StreamingResponse(
            stream_job_sse(container, handle.job_id),
            media_type="text/event-stream",
            headers={"X-Job-Id": handle.job_id, "Cache-Control": "no-cache"},
        )

    result = await submit_and_wait(
        container, kind="review", prompt=prompt, config=config,
        session_id=session_id, resume=resume, stateless=stateless, owner=owner,
    )
    if result.get("status") != JobState.COMPLETED.value:
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(
                error=result.get("error", "review failed"), job_id=result.get("job_id")
            ).model_dump(),
        )
    return ChatResponse(
        job_id=result["job_id"],
        session_id=result.get("session_id"),
        response=result.get("response", ""),
        usage=Usage.from_sdk(result.get("usage")),
        cost_usd=result.get("cost_usd"),
        duration_ms=result.get("duration_ms"),
        num_turns=result.get("num_turns"),
        model=result.get("model"),
        backend=container.backend.name,
    )


def _build_judge_prompt(req: JudgeRequest) -> str:
    parts = []
    if req.question:
        parts.append(f"# Question / Task\n{req.question}")
    parts.append(f"# Candidate Answer\n{req.candidate}")
    if req.evidence:
        ev = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(req.evidence))
        parts.append(f"# Evidence\n{ev}")
    if req.criteria:
        parts.append(f"# Criteria / Rubric\n{req.criteria}")
    parts.append(
        "# Instructions\nEvaluate the candidate against the evidence and criteria. "
        "Return your judgment as JSON with fields: verdict (string), confidence "
        "(number 0-1), reasoning (string), key_factors (array of strings)."
    )
    return "\n\n".join(parts)


def _parse_judgment(result: dict[str, Any]) -> dict[str, Any]:
    """Extract a structured judgment, with graceful fallbacks."""
    so = result.get("structured_output")
    if isinstance(so, dict) and "verdict" in so:
        return so
    text = (result.get("response") or "").strip()
    # Try direct JSON, then JSON embedded in a fenced block.
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except (ValueError, TypeError):
            continue
    # Last resort: treat the whole response as the verdict text.
    return {"verdict": text or "undetermined", "confidence": 0.5, "reasoning": text}


def _json_candidates(text: str):
    yield text
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                yield chunk
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        yield text[start : end + 1]


@router.post("/v1/judge", response_model=JudgeResponse)
async def judge(req: JudgeRequest, request: Request, owner: str = Depends(require_auth)):
    container = get_container(request)
    prompt = _build_judge_prompt(req)
    config = RunConfig(
        model=req.model,
        system_prompt=JUDGE_SYSTEM,
        use_claude_code_preset=False,
        tools=[],
        # Structured-output runs need a little headroom: the model answers and
        # then the result is formatted to the schema. max_turns=1 trips an
        # "error_max_turns" result, so give a small bounded budget.
        max_turns=4,
        output_format=JUDGE_SCHEMA,
        setting_sources=[],  # isolated, deterministic judging
    )
    container.audit.record(
        "job_submit", owner=owner, client=client_ip(request), kind="judge"
    )
    result = await submit_and_wait(
        container, kind="judge", prompt=prompt, config=config, stateless=True, owner=owner,
    )
    if result.get("status") != JobState.COMPLETED.value:
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(
                error=result.get("error", "judge failed"), job_id=result.get("job_id")
            ).model_dump(),
        )
    judgment = _parse_judgment(result)
    confidence = judgment.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (ValueError, TypeError):
        confidence = 0.5
    return JudgeResponse(
        job_id=result["job_id"],
        verdict=str(judgment.get("verdict", "undetermined")),
        confidence=confidence,
        reasoning=str(judgment.get("reasoning", "")),
        key_factors=list(judgment.get("key_factors", []) or []),
        usage=Usage.from_sdk(result.get("usage")),
        cost_usd=result.get("cost_usd"),
    )

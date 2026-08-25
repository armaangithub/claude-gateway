"""Pydantic request/response schemas and shared enums.

These define the public HTTP contract (and therefore the OpenAPI docs at
``/docs``). Internal dataclasses (Job, SessionRecord, GatewayEvent) live next
to the components that own them.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)


class EventType(str, enum.Enum):
    JOB_STARTED = "job.started"
    JOB_MESSAGE = "job.message"
    JOB_THINKING = "job.thinking"
    JOB_TOOL_CALL = "job.tool_call"
    JOB_TOOL_RESULT = "job.tool_result"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------
class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_sdk(cls, raw: dict[str, Any] | None) -> "Usage":
        raw = raw or {}
        inp = int(raw.get("input_tokens", 0) or 0)
        out = int(raw.get("output_tokens", 0) or 0)
        cr = int(raw.get("cache_read_input_tokens", 0) or 0)
        cc = int(raw.get("cache_creation_input_tokens", 0) or 0)
        return cls(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=cr,
            cache_creation_input_tokens=cc,
            total_tokens=inp + out + cr + cc,
        )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    prompt: str = Field(..., description="The user prompt / instruction.")
    session_id: str | None = Field(
        default=None,
        description="Reuse an existing session (multi-turn). Omit to create one.",
    )
    model: str | None = Field(default=None, description="Model alias or id override.")
    system_prompt: str | None = Field(default=None)
    stream: bool = Field(
        default=False,
        description="If true, POST returns a Server-Sent-Events stream instead "
        "of a single JSON body.",
    )
    max_turns: int | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(ChatRequest):
    """Agentic request: tools enabled, runs in an isolated workspace."""

    allowed_tools: list[str] | None = Field(
        default=None,
        description="Tool names the agent may use without prompting. Defaults to "
        "the standard Claude Code toolset.",
    )
    disallowed_tools: list[str] | None = None
    permission_mode: (
        Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
        | None
    ) = None
    workspace: str | None = Field(
        default=None, description="Absolute path to run in. Defaults to a managed, "
        "per-session workspace directory."
    )
    max_budget_usd: float | None = None
    setting_sources: list[str] | None = Field(
        default=None,
        description="Which settings to load: subset of ['user','project','local']. "
        "Omit to load all (full local Claude Code power: your skills, subagents, "
        "and CLAUDE.md).",
    )
    skills: list[str] | str | None = Field(
        default=None, description="Skills to enable: 'all', a list, or omit for 'all'."
    )


class CodeRequest(AgentRequest):
    """Coding-focused agentic request (same shape as AgentRequest)."""


class ReviewRequest(BaseModel):
    prompt: str | None = Field(
        default=None, description="Optional extra instruction for the reviewer."
    )
    diff: str | None = Field(default=None, description="A unified diff to review.")
    code: str | None = Field(default=None, description="A code blob to review.")
    files: list[str] | None = Field(
        default=None, description="Absolute file paths to read and review (read-only)."
    )
    focus: list[str] | None = Field(
        default=None, description="Dimensions to focus on, e.g. ['security','bugs']."
    )
    session_id: str | None = None
    model: str | None = None
    stream: bool = False


class JudgeRequest(BaseModel):
    """SAMURAI Final Judge contract."""

    candidate: str = Field(..., description="The candidate answer/artifact to judge.")
    evidence: list[str] = Field(
        default_factory=list, description="Supporting evidence items."
    )
    question: str | None = Field(
        default=None, description="The original question/task the candidate addresses."
    )
    criteria: str | None = Field(
        default=None, description="Optional rubric the judge should apply."
    )
    model: str | None = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class ChatResponse(BaseModel):
    job_id: str
    session_id: str | None
    response: str
    usage: Usage
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    model: str | None = None
    status: JobState = JobState.COMPLETED
    backend: str | None = None


class ToolCallView(BaseModel):
    name: str
    arguments: dict[str, Any]
    result_summary: str | None = None
    execution_ms: int | None = None
    is_error: bool = False


class AgentResponse(ChatResponse):
    tool_calls: list[ToolCallView] = Field(default_factory=list)


class JudgeResponse(BaseModel):
    job_id: str
    verdict: str = Field(..., description="e.g. 'accept' | 'reject' | free text verdict.")
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = ""
    key_factors: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None


class JobView(BaseModel):
    job_id: str
    session_id: str | None
    kind: str
    status: JobState
    prompt: str
    response: str | None = None
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    backend: str | None = None


class SessionView(BaseModel):
    session_id: str
    title: str | None = None
    kind: str
    workspace: str
    created_at: float
    last_activity: float
    message_count: int = 0
    total_cost_usd: float = 0.0
    warm: bool = False


class ModelInfo(BaseModel):
    id: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class ModelsResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


class HealthResponse(BaseModel):
    status: str
    version: str
    backend: str
    backend_available: bool
    sdk_version: str | None = None
    cli_version: str | None = None
    active_sessions: int = 0
    warm_sessions: int = 0
    running_jobs: int = 0
    uptime_s: float = 0.0


class CreateSessionRequest(BaseModel):
    title: str | None = None
    kind: str = "chat"
    workspace: str | None = None
    model: str | None = None
    system_prompt: str | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    job_id: str | None = None

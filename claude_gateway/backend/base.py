"""Backend interface, run configuration, and result types."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .events import GatewayEvent


@dataclass
class RunConfig:
    """Per-run knobs translated into backend-native options."""

    model: str | None = None
    system_prompt: str | None = None
    use_claude_code_preset: bool = False
    """When True, use Claude Code's full system prompt + default tools (real
    'Claude Code' behavior). When False, use a lean custom prompt (cheaper,
    good for chat/judge)."""
    append_system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    tools: list[str] | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    effort: str | None = None
    stream: bool = False
    cwd: str | None = None
    add_dirs: list[str] = field(default_factory=list)
    output_format: dict[str, Any] | None = None
    setting_sources: list[str] | None = None
    skills: list[str] | str | None = None
    """Enable skills: "all", a list of names, or None (no SDK auto-config)."""


@dataclass
class RunResult:
    """Final result of a run, embedded in the JOB_COMPLETED event."""

    response: str = ""
    session_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    is_error: bool = False
    structured_output: Any = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "response": self.response,
            "session_id": self.session_id,
            "model": self.model,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_input_tokens": self.cache_read_tokens,
                "cache_creation_input_tokens": self.cache_creation_tokens,
                "total_tokens": self.total_tokens,
            },
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "num_turns": self.num_turns,
            "is_error": self.is_error,
            "structured_output": self.structured_output,
            "tool_calls": self.tool_calls,
        }


class ClaudeBackend(abc.ABC):
    """Abstract execution backend."""

    name: str = "base"

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Whether this backend can run right now (binary/auth present)."""

    @abc.abstractmethod
    def run(
        self,
        *,
        prompt: str,
        config: RunConfig,
        session_id: str | None = None,
        resume: bool = False,
        stateless: bool = False,
        cancel_token: "CancelToken | None" = None,
    ) -> AsyncIterator[GatewayEvent]:
        """Execute a prompt, yielding GatewayEvents ending in completed/failed.

        Args:
            prompt: the user instruction.
            config: run configuration.
            session_id: target session UUID (for warm-pool reuse / id control).
            resume: resume the on-disk transcript for ``session_id``.
            stateless: run a fresh, isolated one-shot (no warm client, no
                shared context) — used by /judge and similar.
            cancel_token: cooperative cancellation handle.
        """

    async def interrupt(self, session_id: str) -> bool:
        """Interrupt an in-flight run for a session. Returns True if handled."""
        return False

    async def close_session(self, session_id: str) -> None:
        """Tear down any warm client for a session."""

    async def aclose(self) -> None:
        """Tear down all warm clients / resources."""

    def version_info(self) -> dict[str, Any]:
        return {"backend": self.name}


class CancelToken:
    """Cooperative cancellation flag shared between JobManager and backend."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

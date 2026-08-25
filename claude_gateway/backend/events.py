"""GatewayEvent — the normalized event both backends emit.

The SDK and CLI backends translate their native message streams into this one
shape so the rest of the system (jobs, streaming, persistence) is
backend-agnostic. Mirrors the event taxonomy the user asked for:
job.started / job.message / job.tool_call / job.tool_result /
job.completed / job.failed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..models import EventType


@dataclass
class GatewayEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_wire(self, seq: int | None = None) -> dict[str, Any]:
        d = {"type": self.type.value, "ts": self.ts, "data": self.data}
        if seq is not None:
            d["seq"] = seq
        return d

    # -- convenience constructors ------------------------------------------
    @classmethod
    def started(cls, job_id: str, session_id: str | None, kind: str) -> "GatewayEvent":
        return cls(
            EventType.JOB_STARTED,
            {"job_id": job_id, "session_id": session_id, "kind": kind},
        )

    @classmethod
    def message(cls, text: str, *, delta: bool = False) -> "GatewayEvent":
        return cls(EventType.JOB_MESSAGE, {"text": text, "delta": delta})

    @classmethod
    def thinking(cls, text: str) -> "GatewayEvent":
        return cls(EventType.JOB_THINKING, {"text": text})

    @classmethod
    def tool_call(cls, name: str, arguments: dict[str, Any], tool_use_id: str) -> "GatewayEvent":
        return cls(
            EventType.JOB_TOOL_CALL,
            {"name": name, "arguments": arguments, "tool_use_id": tool_use_id},
        )

    @classmethod
    def tool_result(
        cls,
        tool_use_id: str,
        result_summary: str,
        *,
        is_error: bool = False,
        execution_ms: int | None = None,
        name: str | None = None,
    ) -> "GatewayEvent":
        return cls(
            EventType.JOB_TOOL_RESULT,
            {
                "tool_use_id": tool_use_id,
                "name": name,
                "result_summary": result_summary,
                "is_error": is_error,
                "execution_ms": execution_ms,
            },
        )

    @classmethod
    def completed(cls, result: dict[str, Any]) -> "GatewayEvent":
        return cls(EventType.JOB_COMPLETED, result)

    @classmethod
    def failed(cls, error: str, *, detail: str | None = None) -> "GatewayEvent":
        return cls(EventType.JOB_FAILED, {"error": error, "detail": detail})

    @classmethod
    def cancelled(cls) -> "GatewayEvent":
        return cls(EventType.JOB_CANCELLED, {})

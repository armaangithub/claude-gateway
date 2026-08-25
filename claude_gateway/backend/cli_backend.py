"""ClaudeCLIBackend — fallback backend that shells out to ``claude -p``.

Used only when the Agent SDK is unavailable (``CLAUDE_BACKEND=cli`` or the SDK
import fails). It spawns ``claude -p --output-format stream-json`` per request
and parses the streamed JSON into GatewayEvents. Session continuity is provided
by the CLI's on-disk transcripts via ``--resume``.

This path *does* spawn a process per request — that is the accepted cost of the
degraded fallback. The primary SDK backend keeps processes warm instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from typing import Any

from ..config import Settings
from ..logging_config import get_logger
from .base import CancelToken, ClaudeBackend, RunConfig, RunResult
from .events import GatewayEvent

log = get_logger("gateway.backend.cli")

for _v in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
    os.environ.pop(_v, None)


class ClaudeCLIBackend(ClaudeBackend):
    name = "cli"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def _claude_path(self) -> str | None:
        return self.settings.claude_cli_path or shutil.which("claude")

    async def is_available(self) -> bool:
        return self._claude_path() is not None

    def version_info(self) -> dict[str, Any]:
        return {"backend": self.name, "claude_path": self._claude_path()}

    async def cli_version(self) -> str | None:
        path = self._claude_path()
        if not path:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return out.decode().strip()
        except Exception:  # pragma: no cover
            return None

    def _build_args(
        self, prompt: str, config: RunConfig, session_id: str | None, resume: bool
    ) -> list[str]:
        path = self._claude_path()
        assert path
        args = [path, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        model = config.model or self.settings.default_model
        if model:
            args += ["--model", model]
        if config.permission_mode:
            args += ["--permission-mode", config.permission_mode]
        if config.allowed_tools:
            args += ["--allowedTools", ",".join(config.allowed_tools)]
        if config.disallowed_tools:
            args += ["--disallowedTools", ",".join(config.disallowed_tools)]
        if config.max_turns is not None:
            args += ["--max-turns", str(config.max_turns)]
        if config.add_dirs:
            for d in config.add_dirs:
                args += ["--add-dir", str(d)]
        if config.use_claude_code_preset and config.append_system_prompt:
            args += ["--append-system-prompt", config.append_system_prompt]
        elif not config.use_claude_code_preset and config.system_prompt:
            args += ["--system-prompt", config.system_prompt]
        if resume and session_id:
            args += ["--resume", session_id]
        elif session_id:
            args += ["--session-id", session_id]
        return args

    async def run(
        self,
        *,
        prompt: str,
        config: RunConfig,
        session_id: str | None = None,
        resume: bool = False,
        stateless: bool = False,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[GatewayEvent]:
        path = self._claude_path()
        if not path:
            yield GatewayEvent.failed("claude CLI not found on PATH")
            return

        args = self._build_args(prompt, config, session_id, resume)
        env = dict(os.environ)
        env["CLAUDE_AGENT_SDK_CLIENT_APP"] = "claude-gateway-cli/1.0.0"
        cwd = config.cwd or None
        started = time.time()
        agg = _CliAggregator(session_id=session_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except Exception as e:
            yield GatewayEvent.failed(f"failed to spawn claude: {e}")
            return

        if session_id:
            self._procs[session_id] = proc
        try:
            assert proc.stdout is not None
            while True:
                if cancel_token and cancel_token.cancelled:
                    proc.terminate()
                    yield GatewayEvent.cancelled()
                    return
                line = await proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for ev in agg.consume(obj):
                    yield ev
            await proc.wait()
        finally:
            if session_id:
                self._procs.pop(session_id, None)

        if proc.returncode not in (0, None) and not agg.got_result:
            stderr = b""
            try:
                stderr = await proc.stderr.read() if proc.stderr else b""
            except Exception:
                pass
            yield GatewayEvent.failed(
                f"claude exited with code {proc.returncode}",
                detail=stderr.decode(errors="replace")[:500],
            )
            return

        result = agg.finalize(default_duration_ms=int((time.time() - started) * 1000))
        if result.is_error:
            yield GatewayEvent.failed(result.response or "run error")
        else:
            yield GatewayEvent.completed(result.as_dict())

    async def interrupt(self, session_id: str) -> bool:
        proc = self._procs.get(session_id)
        if proc is None:
            return False
        try:
            proc.terminate()
            return True
        except Exception:  # pragma: no cover
            return False

    async def aclose(self) -> None:
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except Exception:
                pass


class _CliAggregator:
    """Parse ``claude`` stream-json objects into GatewayEvents + a RunResult."""

    def __init__(self, session_id: str | None = None) -> None:
        self.result = RunResult(session_id=session_id)
        self._text_parts: list[str] = []
        self._tool_starts: dict[str, dict[str, Any]] = {}
        self.got_result = False

    def consume(self, obj: dict[str, Any]) -> list[GatewayEvent]:
        out: list[GatewayEvent] = []
        t = obj.get("type")
        if t == "system":
            if obj.get("session_id"):
                self.result.session_id = obj["session_id"]
            return out
        if t == "assistant":
            message = obj.get("message", {})
            self.result.model = message.get("model") or self.result.model
            for block in message.get("content", []):
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    self._text_parts.append(block["text"])
                    out.append(GatewayEvent.message(block["text"]))
                elif bt == "thinking" and block.get("thinking"):
                    out.append(GatewayEvent.thinking(block["thinking"]))
                elif bt == "tool_use":
                    tid = block.get("id", "")
                    self._tool_starts[tid] = {
                        "name": block.get("name"),
                        "arguments": block.get("input", {}),
                        "start": time.time(),
                    }
                    out.append(
                        GatewayEvent.tool_call(
                            block.get("name", ""), block.get("input", {}), tid
                        )
                    )
            return out
        if t == "user":
            message = obj.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        out.append(self._tool_result_event(block))
            return out
        if t == "result":
            self._apply_result(obj)
            self.got_result = True
            return out
        if t == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    out.append(GatewayEvent.message(delta["text"], delta=True))
            return out
        return out

    def _tool_result_event(self, block: dict[str, Any]) -> GatewayEvent:
        tid = block.get("tool_use_id", "")
        start = self._tool_starts.get(tid, {})
        exec_ms = int((time.time() - start["start"]) * 1000) if "start" in start else None
        from .sdk_backend import _summarize_tool_result

        summary = _summarize_tool_result(block.get("content"))
        self.result.tool_calls.append(
            {
                "name": start.get("name"),
                "arguments": start.get("arguments", {}),
                "result_summary": summary,
                "execution_ms": exec_ms,
                "is_error": bool(block.get("is_error")),
                "tool_use_id": tid,
            }
        )
        return GatewayEvent.tool_result(
            tid, summary, is_error=bool(block.get("is_error")),
            execution_ms=exec_ms, name=start.get("name"),
        )

    def _apply_result(self, obj: dict[str, Any]) -> None:
        r = self.result
        if obj.get("session_id"):
            r.session_id = obj["session_id"]
        r.cost_usd = obj.get("total_cost_usd")
        r.duration_ms = obj.get("duration_ms")
        r.num_turns = obj.get("num_turns")
        r.is_error = bool(obj.get("is_error"))
        u = obj.get("usage") or {}
        r.input_tokens = int(u.get("input_tokens", 0) or 0)
        r.output_tokens = int(u.get("output_tokens", 0) or 0)
        r.cache_read_tokens = int(u.get("cache_read_input_tokens", 0) or 0)
        r.cache_creation_tokens = int(u.get("cache_creation_input_tokens", 0) or 0)
        r.total_tokens = (
            r.input_tokens + r.output_tokens + r.cache_read_tokens + r.cache_creation_tokens
        )
        if obj.get("result"):
            r.response = obj["result"]
        elif self._text_parts:
            r.response = "".join(self._text_parts)

    def finalize(self, *, default_duration_ms: int) -> RunResult:
        if not self.result.response and self._text_parts:
            self.result.response = "".join(self._text_parts)
        if self.result.duration_ms is None:
            self.result.duration_ms = default_duration_ms
        return self.result

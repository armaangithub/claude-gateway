"""ClaudeSDKBackend — primary backend built on the Claude Agent SDK.

Design highlights:

* **Warm client pool.** A live :class:`ClaudeSDKClient` (and its long-running
  ``claude`` subprocess) is kept per session, so follow-up turns reuse the same
  process — no spawn per request. An idle reaper tears them down after a TTL.
* **Native session persistence.** New sessions are created with a caller-chosen
  UUID via ``options.session_id``; after a restart/reap the same UUID is
  reloaded with ``options.resume``. The CLI writes the transcript to disk, so
  sessions survive restarts for free.
* **Stateless mode.** ``stateless=True`` runs a fresh one-shot via ``query()``
  with isolated context — used by /judge so judgments never bleed together.
* **Normalized events.** SDK messages are translated into GatewayEvents.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..logging_config import get_logger
from .base import CancelToken, ClaudeBackend, RunConfig, RunResult
from .events import GatewayEvent

log = get_logger("gateway.backend.sdk")

# Scrub Claude Code's own session markers from the environment we hand to the
# spawned subprocess. If the gateway is (during development) launched from
# inside a Claude Code session, these would confuse the nested process.
for _v in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
    os.environ.pop(_v, None)


@dataclass
class _LiveClient:
    client: Any  # claude_agent_sdk.ClaudeSDKClient
    session_id: str
    lock: asyncio.Lock
    last_used: float
    fingerprint: str = ""
    busy: bool = False


class ClaudeSDKBackend(ClaudeBackend):
    name = "sdk"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._live: dict[str, _LiveClient] = {}
        self._pool_lock = asyncio.Lock()
        self._sdk = None  # lazy import
        self._reaper_task: asyncio.Task | None = None

    # ---- availability -----------------------------------------------------
    def _import_sdk(self):
        if self._sdk is None:
            import claude_agent_sdk as sdk  # noqa: PLC0415

            self._sdk = sdk
        return self._sdk

    async def is_available(self) -> bool:
        try:
            self._import_sdk()
            return True
        except Exception as e:  # pragma: no cover
            log.warning("SDK import failed: %s", e)
            return False

    def version_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": self.name}
        try:
            sdk = self._import_sdk()
            info["sdk_version"] = getattr(sdk, "__version__", None)
            try:
                from claude_agent_sdk import _cli_version  # type: ignore

                info["bundled_cli_version"] = getattr(
                    _cli_version, "__cli_version__", None
                )
            except Exception:
                pass
        except Exception:
            info["sdk_version"] = None
        return info

    # ---- options builder --------------------------------------------------
    def _build_options(
        self, config: RunConfig, *, session_id: str | None, resume: bool
    ):
        sdk = self._import_sdk()
        kwargs: dict[str, Any] = {}

        model = config.model or self.settings.default_model
        if model:
            kwargs["model"] = model

        # System prompt + tool surface.
        if config.use_claude_code_preset:
            preset: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
            if config.append_system_prompt:
                preset["append"] = config.append_system_prompt
            kwargs["system_prompt"] = preset
            if config.tools is not None:
                kwargs["tools"] = config.tools
        else:
            if config.system_prompt:
                kwargs["system_prompt"] = config.system_prompt
            # Default to no built-in tools for lean (chat/judge) runs unless
            # the caller explicitly opts into a tool list.
            kwargs["tools"] = config.tools if config.tools is not None else []

        if config.allowed_tools:
            kwargs["allowed_tools"] = config.allowed_tools
        if config.disallowed_tools:
            kwargs["disallowed_tools"] = config.disallowed_tools
        if config.permission_mode:
            kwargs["permission_mode"] = config.permission_mode
        if config.max_turns is not None:
            kwargs["max_turns"] = config.max_turns
        if config.max_budget_usd is not None:
            kwargs["max_budget_usd"] = config.max_budget_usd
        if config.effort:
            kwargs["effort"] = config.effort
        if config.stream:
            kwargs["include_partial_messages"] = True
        if config.cwd:
            kwargs["cwd"] = config.cwd
        if config.add_dirs:
            kwargs["add_dirs"] = config.add_dirs
        if config.output_format:
            kwargs["output_format"] = config.output_format
        if config.setting_sources is not None:
            kwargs["setting_sources"] = config.setting_sources
        if config.skills is not None:
            kwargs["skills"] = config.skills
        if self.settings.claude_cli_path:
            kwargs["cli_path"] = self.settings.claude_cli_path

        # Identify ourselves + harden the subprocess env.
        kwargs["env"] = {"CLAUDE_AGENT_SDK_CLIENT_APP": "claude-gateway/1.0.0"}

        # Session control: resume xor session_id (cannot set both w/o fork).
        if resume and session_id:
            kwargs["resume"] = session_id
        elif session_id:
            kwargs["session_id"] = session_id

        return sdk.ClaudeAgentOptions(**kwargs)

    @staticmethod
    def _fingerprint(config: RunConfig) -> str:
        return "|".join(
            str(x)
            for x in (
                config.model,
                config.use_claude_code_preset,
                config.permission_mode,
                tuple(config.allowed_tools),
                config.tools,
                config.cwd,
            )
        )

    # ---- run --------------------------------------------------------------
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
        if stateless:
            async for ev in self._run_stateless(
                prompt, config, session_id, cancel_token
            ):
                yield ev
        else:
            async for ev in self._run_stateful(
                prompt, config, session_id, resume, cancel_token
            ):
                yield ev

    # ---- stateless (one-shot, isolated) ----------------------------------
    async def _run_stateless(
        self,
        prompt: str,
        config: RunConfig,
        session_id: str | None,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[GatewayEvent]:
        sdk = self._import_sdk()
        options = self._build_options(config, session_id=session_id, resume=False)
        agg = _Aggregator()
        started = time.time()
        try:
            async for msg in sdk.query(prompt=prompt, options=options):
                if cancel_token and cancel_token.cancelled:
                    yield GatewayEvent.cancelled()
                    return
                for ev in agg.consume(msg):
                    yield ev
        except Exception as e:
            yield GatewayEvent.failed(_err_str(e), detail=repr(e))
            return
        result = agg.finalize(default_duration_ms=int((time.time() - started) * 1000))
        if result.is_error:
            yield GatewayEvent.failed(result.response or "run error")
        else:
            yield GatewayEvent.completed(result.as_dict())

    # ---- stateful (warm pool) --------------------------------------------
    async def _get_or_create_live(
        self, session_id: str, config: RunConfig, resume: bool
    ) -> _LiveClient:
        sdk = self._import_sdk()
        async with self._pool_lock:
            live = self._live.get(session_id)
            if live is not None:
                return live
            # Create a fresh live client.
            options = self._build_options(
                config, session_id=session_id, resume=resume
            )
            client = sdk.ClaudeSDKClient(options=options)
            try:
                await client.connect()
            except Exception as e:
                if resume:
                    # Resume failed (transcript gone?). Retry fresh, same id.
                    log.warning(
                        "resume failed for %s (%s); starting fresh", session_id, e
                    )
                    options = self._build_options(
                        config, session_id=session_id, resume=False
                    )
                    client = sdk.ClaudeSDKClient(options=options)
                    await client.connect()
                else:
                    raise
            live = _LiveClient(
                client=client,
                session_id=session_id,
                lock=asyncio.Lock(),
                last_used=time.time(),
                fingerprint=self._fingerprint(config),
            )
            self._live[session_id] = live
            self._ensure_reaper()
            return live

    async def _run_stateful(
        self,
        prompt: str,
        config: RunConfig,
        session_id: str | None,
        resume: bool,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[GatewayEvent]:
        if not session_id:
            raise ValueError("stateful run requires a session_id")
        try:
            live = await self._get_or_create_live(session_id, config, resume)
        except Exception as e:
            yield GatewayEvent.failed(_err_str(e), detail=repr(e))
            return

        agg = _Aggregator(session_id=session_id)
        started = time.time()
        async with live.lock:
            live.busy = True
            try:
                await live.client.query(prompt)
                async for msg in live.client.receive_response():
                    if cancel_token and cancel_token.cancelled:
                        try:
                            await live.client.interrupt()
                        except Exception:
                            pass
                        yield GatewayEvent.cancelled()
                        live.busy = False
                        live.last_used = time.time()
                        return
                    for ev in agg.consume(msg):
                        yield ev
            except Exception as e:
                # A live client that errored may be in a bad state — drop it.
                live.busy = False
                await self._drop_live(session_id)
                yield GatewayEvent.failed(_err_str(e), detail=repr(e))
                return
            live.busy = False
            live.last_used = time.time()

        result = agg.finalize(default_duration_ms=int((time.time() - started) * 1000))
        result.session_id = result.session_id or session_id
        if result.is_error:
            yield GatewayEvent.failed(result.response or "run error")
        else:
            yield GatewayEvent.completed(result.as_dict())

    # ---- lifecycle --------------------------------------------------------
    async def interrupt(self, session_id: str) -> bool:
        live = self._live.get(session_id)
        if live is None:
            return False
        try:
            await live.client.interrupt()
            return True
        except Exception as e:  # pragma: no cover
            log.warning("interrupt failed for %s: %s", session_id, e)
            return False

    async def _drop_live(self, session_id: str) -> None:
        async with self._pool_lock:
            live = self._live.pop(session_id, None)
        if live is not None:
            try:
                await live.client.disconnect()
            except Exception:
                pass

    async def close_session(self, session_id: str) -> None:
        await self._drop_live(session_id)

    async def aclose(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
        ids = list(self._live.keys())
        for sid in ids:
            await self._drop_live(sid)

    def warm_session_ids(self) -> list[str]:
        return list(self._live.keys())

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            try:
                self._reaper_task = asyncio.create_task(self._reap_loop())
            except RuntimeError:  # no running loop (tests) — skip
                pass

    async def _reap_loop(self) -> None:
        ttl = self.settings.session_idle_ttl_s
        try:
            while True:
                await asyncio.sleep(min(60, max(10, ttl // 4)))
                now = time.time()
                stale = [
                    sid
                    for sid, lv in list(self._live.items())
                    if not lv.busy and (now - lv.last_used) > ttl
                ]
                for sid in stale:
                    log.info("reaping idle session %s", sid)
                    await self._drop_live(sid)
        except asyncio.CancelledError:  # pragma: no cover
            pass


class _Aggregator:
    """Translate SDK messages into GatewayEvents and accumulate a RunResult."""

    def __init__(self, session_id: str | None = None) -> None:
        self.result = RunResult(session_id=session_id)
        self._text_parts: list[str] = []
        self._tool_starts: dict[str, dict[str, Any]] = {}
        self._sdk = None

    def _types(self):
        if self._sdk is None:
            import claude_agent_sdk as sdk  # noqa: PLC0415

            self._sdk = sdk
        return self._sdk

    def consume(self, msg: Any) -> list[GatewayEvent]:
        sdk = self._types()
        out: list[GatewayEvent] = []

        if isinstance(msg, sdk.SystemMessage):
            sid = (msg.data or {}).get("session_id")
            if sid:
                self.result.session_id = sid
            return out

        if isinstance(msg, sdk.StreamEvent):
            ev = msg.event or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    out.append(GatewayEvent.message(delta["text"], delta=True))
            return out

        if isinstance(msg, sdk.AssistantMessage):
            if msg.model:
                self.result.model = msg.model
            for block in msg.content:
                if isinstance(block, sdk.TextBlock):
                    self._text_parts.append(block.text)
                    out.append(GatewayEvent.message(block.text, delta=False))
                elif isinstance(block, sdk.ThinkingBlock):
                    if block.thinking:
                        out.append(GatewayEvent.thinking(block.thinking))
                elif isinstance(block, sdk.ToolUseBlock):
                    self._tool_starts[block.id] = {
                        "name": block.name,
                        "arguments": block.input,
                        "start": time.time(),
                    }
                    out.append(
                        GatewayEvent.tool_call(block.name, block.input, block.id)
                    )
                elif isinstance(block, getattr(sdk, "ServerToolUseBlock", ())):
                    out.append(
                        GatewayEvent.tool_call(block.name, block.input, block.id)
                    )
            return out

        if isinstance(msg, sdk.UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, sdk.ToolResultBlock):
                        out.append(self._tool_result_event(sdk, block))
            return out

        if isinstance(msg, sdk.ResultMessage):
            self._apply_result(msg)
            return out

        return out

    def _tool_result_event(self, sdk, block) -> GatewayEvent:
        start = self._tool_starts.get(block.tool_use_id, {})
        exec_ms = (
            int((time.time() - start["start"]) * 1000) if "start" in start else None
        )
        summary = _summarize_tool_result(block.content)
        rec = {
            "name": start.get("name"),
            "arguments": start.get("arguments", {}),
            "result_summary": summary,
            "execution_ms": exec_ms,
            "is_error": bool(block.is_error),
            "tool_use_id": block.tool_use_id,
        }
        self.result.tool_calls.append(rec)
        return GatewayEvent.tool_result(
            block.tool_use_id,
            summary,
            is_error=bool(block.is_error),
            execution_ms=exec_ms,
            name=start.get("name"),
        )

    def _apply_result(self, msg: Any) -> None:
        r = self.result
        if msg.session_id:
            r.session_id = msg.session_id
        r.cost_usd = msg.total_cost_usd
        r.duration_ms = msg.duration_ms
        r.num_turns = msg.num_turns
        r.is_error = bool(msg.is_error)
        r.structured_output = getattr(msg, "structured_output", None)
        u = msg.usage or {}
        r.input_tokens = int(u.get("input_tokens", 0) or 0)
        r.output_tokens = int(u.get("output_tokens", 0) or 0)
        r.cache_read_tokens = int(u.get("cache_read_input_tokens", 0) or 0)
        r.cache_creation_tokens = int(u.get("cache_creation_input_tokens", 0) or 0)
        r.total_tokens = (
            r.input_tokens
            + r.output_tokens
            + r.cache_read_tokens
            + r.cache_creation_tokens
        )
        # Prefer the CLI's final result text; fall back to accumulated text.
        if getattr(msg, "result", None):
            r.response = msg.result
        elif self._text_parts:
            r.response = "".join(self._text_parts)

    def finalize(self, *, default_duration_ms: int) -> RunResult:
        if not self.result.response and self._text_parts:
            self.result.response = "".join(self._text_parts)
        if self.result.duration_ms is None:
            self.result.duration_ms = default_duration_ms
        return self.result


def _summarize_tool_result(content: Any, limit: int = 280) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or str(item))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    else:
        text = str(content)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def _err_str(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    return msg

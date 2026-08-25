"""Shared test fixtures and a FakeBackend (so tests never spawn real Claude)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from claude_gateway.backend.base import CancelToken, ClaudeBackend, RunConfig
from claude_gateway.backend.events import GatewayEvent
from claude_gateway.config import Settings, set_settings


class FakeBackend(ClaudeBackend):
    """Deterministic in-process backend that mimics the event shapes the real
    backends emit, without any subprocess or network call."""

    name = "fake"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._warm: set[str] = set()
        self.calls: list[dict] = []

    async def is_available(self) -> bool:
        return True

    def version_info(self):
        return {"backend": "fake", "sdk_version": "fake-0.0.0"}

    def warm_session_ids(self):
        return list(self._warm)

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
        self.calls.append(
            {
                "prompt": prompt,
                "session_id": session_id,
                "resume": resume,
                "stateless": stateless,
                "preset": config.use_claude_code_preset,
                "output_format": config.output_format,
            }
        )
        if not stateless and session_id:
            self._warm.add(session_id)

        if cancel_token and cancel_token.cancelled:
            yield GatewayEvent.cancelled()
            return

        # Judge: emit structured output.
        if config.output_format is not None:
            yield GatewayEvent.message('{"verdict":"accept"}')
            yield GatewayEvent.completed(
                {
                    "response": '{"verdict":"accept","confidence":0.91,'
                    '"reasoning":"evidence supports it","key_factors":["a","b"]}',
                    "session_id": session_id,
                    "model": "fake-model",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "total_tokens": 15,
                    },
                    "cost_usd": 0.0012,
                    "duration_ms": 5,
                    "num_turns": 1,
                    "is_error": False,
                    "structured_output": {
                        "verdict": "accept",
                        "confidence": 0.91,
                        "reasoning": "evidence supports it",
                        "key_factors": ["a", "b"],
                    },
                    "tool_calls": [],
                }
            )
            return

        # Agent/code: emit a tool call + result.
        tool_calls = []
        if config.use_claude_code_preset:
            yield GatewayEvent.tool_call("Read", {"file_path": "x.py"}, "tu_1")
            yield GatewayEvent.tool_result("tu_1", "file contents", name="Read", execution_ms=3)
            tool_calls = [
                {
                    "name": "Read",
                    "arguments": {"file_path": "x.py"},
                    "result_summary": "file contents",
                    "execution_ms": 3,
                    "is_error": False,
                    "tool_use_id": "tu_1",
                }
            ]

        reply = f"echo: {prompt[:40]}"
        yield GatewayEvent.message(reply)
        yield GatewayEvent.completed(
            {
                "response": reply,
                "session_id": session_id,
                "model": "fake-model",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 20,
                },
                "cost_usd": 0.0021,
                "duration_ms": 7,
                "num_turns": 1,
                "is_error": False,
                "tool_calls": tool_calls,
            }
        )

    async def interrupt(self, session_id: str) -> bool:
        return session_id in self._warm

    async def close_session(self, session_id: str) -> None:
        self._warm.discard(session_id)

    async def aclose(self) -> None:
        self._warm.clear()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        API_KEY="",  # auth disabled by default
        HOST="127.0.0.1",
        PORT=0,
        GATEWAY_HOME=str(tmp_path / "home"),
        RATE_LIMIT_PER_MIN=0,  # disabled by default
        CLAUDE_BACKEND="auto",
    )
    set_settings(s)
    return s


@pytest.fixture
def fake_backend(settings):
    return FakeBackend(settings)


@pytest.fixture
def client(settings, fake_backend, monkeypatch):
    """A TestClient with the FakeBackend injected via build_backend patch."""
    from fastapi.testclient import TestClient

    import claude_gateway.container as container_mod
    from claude_gateway.app import create_app

    async def _fake_build_backend(_settings):
        return fake_backend

    monkeypatch.setattr(container_mod, "build_backend", _fake_build_backend)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    """Build TestClients with arbitrary settings overrides (auth, rate limits)."""
    from fastapi.testclient import TestClient

    import claude_gateway.container as container_mod
    from claude_gateway.app import create_app

    created: list[TestClient] = []

    def _make(**overrides):
        opts = dict(
            API_KEY="",
            HOST="127.0.0.1",
            PORT=0,
            GATEWAY_HOME=str(tmp_path / f"home{len(created)}"),
            RATE_LIMIT_PER_MIN=0,
            CLAUDE_BACKEND="auto",
        )
        opts.update(overrides)
        s = Settings(**opts)
        set_settings(s)
        fb = FakeBackend(s)

        async def _b(_s):
            return fb

        monkeypatch.setattr(container_mod, "build_backend", _b)
        app = create_app(s)
        tc = TestClient(app)
        tc.__enter__()
        created.append(tc)
        return tc, fb

    yield _make
    for tc in created:
        tc.__exit__(None, None, None)

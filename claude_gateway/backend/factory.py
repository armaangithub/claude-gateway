"""Backend selection.

``CLAUDE_BACKEND``:
    sdk   -> force the Agent SDK backend
    cli   -> force the ``claude -p`` fallback
    auto  -> prefer the SDK; fall back to the CLI if the SDK can't import
"""

from __future__ import annotations

from ..config import Settings
from ..logging_config import get_logger
from .base import ClaudeBackend

log = get_logger("gateway.backend.factory")


async def build_backend(settings: Settings) -> ClaudeBackend:
    from .cli_backend import ClaudeCLIBackend
    from .sdk_backend import ClaudeSDKBackend

    choice = settings.backend
    if choice == "cli":
        backend: ClaudeBackend = ClaudeCLIBackend(settings)
        if not await backend.is_available():
            raise RuntimeError("CLAUDE_BACKEND=cli but the claude CLI was not found")
        log.info("backend: CLI (forced)")
        return backend

    sdk = ClaudeSDKBackend(settings)
    if choice == "sdk":
        if not await sdk.is_available():
            raise RuntimeError(
                "CLAUDE_BACKEND=sdk but claude-agent-sdk is not importable"
            )
        log.info("backend: SDK (forced) %s", sdk.version_info())
        return sdk

    # auto
    if await sdk.is_available():
        log.info("backend: SDK (auto) %s", sdk.version_info())
        return sdk
    cli = ClaudeCLIBackend(settings)
    if await cli.is_available():
        log.warning("backend: CLI (auto fallback — SDK unavailable)")
        return cli
    raise RuntimeError(
        "No backend available: neither claude-agent-sdk nor the claude CLI was found"
    )

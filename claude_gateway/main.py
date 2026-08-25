"""Uvicorn entrypoint. ``python -m claude_gateway.main`` or via the CLI."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import get_settings


def main() -> None:
    settings = get_settings()
    if (
        not settings.localhost_only
        and not settings.allow_remote
        and settings.host not in ("127.0.0.1", "::1", "localhost")
    ):
        raise SystemExit(
            f"Refusing to bind to {settings.host}: set ALLOW_REMOTE=1 to allow "
            "non-loopback binding, or LOCALHOST_ONLY=0."
        )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

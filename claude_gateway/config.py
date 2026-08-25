"""Configuration — all settings are environment-driven with sane localhost defaults.

Env vars (prefix optional ``GATEWAY_`` also accepted):
    CLAUDE_BACKEND   sdk | cli | auto      (default: auto)
    HOST             bind address          (default: 127.0.0.1)
    PORT             bind port             (default: 8080)
    API_KEY          shared secret         (default: changeme; "" disables auth)
    ...see field docstrings below.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_home() -> Path:
    return Path(os.environ.get("GATEWAY_HOME", str(Path.home() / ".claude_gateway")))


class Settings(BaseSettings):
    """Runtime configuration. Reads from environment and an optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("GATEWAY_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Accept both bare (HOST) and prefixed (GATEWAY_HOST) names; bare wins.
        env_prefix="",
    )

    # ---- Server -----------------------------------------------------------
    host: str = Field(default="127.0.0.1", validation_alias="HOST")
    port: int = Field(default=8080, validation_alias="PORT")
    localhost_only: bool = Field(default=True, validation_alias="LOCALHOST_ONLY")
    """When True, refuse to bind to a non-loopback address unless ALLOW_REMOTE=1."""
    allow_remote: bool = Field(default=False, validation_alias="ALLOW_REMOTE")

    # ---- Backend ----------------------------------------------------------
    backend: Literal["sdk", "cli", "auto"] = Field(
        default="auto", validation_alias="CLAUDE_BACKEND"
    )
    claude_cli_path: str | None = Field(default=None, validation_alias="CLAUDE_CLI_PATH")
    """Explicit path to the ``claude`` binary. If unset the SDK uses its bundled
    CLI and the CLI-fallback backend searches PATH."""
    default_model: str | None = Field(
        default="claude-opus-4-8", validation_alias="DEFAULT_MODEL"
    )
    """Model alias/id passed to the SDK for every request that doesn't override
    it. Defaults to Opus 4.8. Set to 'opus' to track the latest Opus, or blank
    to use Claude Code's own default."""
    default_permission_mode: str = Field(
        default="auto", validation_alias="DEFAULT_PERMISSION_MODE"
    )
    """Permission mode for agent/code endpoints. 'auto' lets a model classifier
    approve/deny each tool call so runs proceed without a human (there is none
    at the HTTP end). Use 'bypassPermissions' to allow everything unconditionally."""
    max_turns: int = Field(default=0, validation_alias="MAX_TURNS")
    """Max conversation turns per run. 0 = unlimited (bounded only by your
    Claude Code plan)."""
    max_budget_usd: float | None = Field(default=None, validation_alias="MAX_BUDGET_USD")
    """Per-run USD ceiling. None = no ceiling (bounded only by your plan)."""
    session_idle_ttl_s: int = Field(
        default=900, validation_alias="SESSION_IDLE_TTL_S"
    )
    """Reap a warm session's live subprocess after this many idle seconds."""
    job_timeout_s: int = Field(default=3600, validation_alias="JOB_TIMEOUT_S")
    """Hard per-job timeout in seconds. Deliberately high — Claude Code can take
    a while on big agent runs. 0 = no timeout (wait indefinitely)."""

    # ---- Storage ----------------------------------------------------------
    home: Path = Field(default_factory=_default_home, validation_alias="GATEWAY_HOME")
    db_path: Path | None = Field(default=None, validation_alias="DB_PATH")
    workspaces_dir: Path | None = Field(default=None, validation_alias="WORKSPACES_DIR")

    # ---- Security ---------------------------------------------------------
    api_key: str = Field(default="changeme", validation_alias="API_KEY")
    """Shared bearer secret. Empty string disables auth (localhost dev only)."""
    rate_limit_per_min: int = Field(default=0, validation_alias="RATE_LIMIT_PER_MIN")
    """Requests/min per identity. 0 = unlimited (default — single-user localhost).
    Set > 0 to enable throttling."""
    rate_limit_burst: int = Field(default=30, validation_alias="RATE_LIMIT_BURST")
    cors_origins: str = Field(default="*", validation_alias="CORS_ORIGINS")
    """Comma-separated list of allowed CORS origins, or '*'."""

    # ---- Observability ----------------------------------------------------
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=False, validation_alias="LOG_JSON")
    otel_enabled: bool = Field(default=False, validation_alias="OTEL_ENABLED")
    otel_endpoint: str | None = Field(default=None, validation_alias="OTEL_ENDPOINT")
    service_name: str = Field(default="claude-gateway", validation_alias="OTEL_SERVICE_NAME")

    # ---- Derived semantics ------------------------------------------------
    def effective_max_turns(self) -> int | None:
        """None = unlimited (don't pass --max-turns)."""
        return self.max_turns if self.max_turns and self.max_turns > 0 else None

    def effective_job_timeout(self) -> float | None:
        """None = no timeout (wait indefinitely)."""
        return float(self.job_timeout_s) if self.job_timeout_s and self.job_timeout_s > 0 else None

    # ---- Derived paths ----------------------------------------------------
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.home / "gateway.db")

    def resolved_workspaces_dir(self) -> Path:
        return self.workspaces_dir or (self.home / "workspaces")

    def pidfile(self) -> Path:
        return self.home / "gateway.pid"

    def audit_log_path(self) -> Path:
        return self.home / "audit.log"

    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.resolved_workspaces_dir().mkdir(parents=True, exist_ok=True)
        self.resolved_db_path().parent.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide singleton settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    """Override settings (used by tests)."""
    global _settings
    _settings = s

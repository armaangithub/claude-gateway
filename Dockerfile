# Local Claude Code Gateway
#
# IMPORTANT — authentication in containers:
#   The host's Claude Code subscription auth lives in the macOS Keychain and is
#   NOT available inside a container. To run the gateway in Docker you must
#   provide an Anthropic API key via ANTHROPIC_API_KEY (the Agent SDK / CLI will
#   use it). On the host (bare-metal `gateway start`) the subscription auth works
#   with no key.
FROM python:3.12-slim

# Node is required for the Claude Code CLI that the Agent SDK drives.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY claude_gateway ./claude_gateway
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

ENV HOST=0.0.0.0 \
    PORT=8080 \
    LOCALHOST_ONLY=0 \
    ALLOW_REMOTE=1 \
    GATEWAY_HOME=/data \
    CLAUDE_BACKEND=auto

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/v1/health || exit 1

CMD ["python", "-m", "claude_gateway.main"]

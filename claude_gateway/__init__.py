"""Local Claude Code Gateway.

Exposes a locally-installed Claude Code (via the Claude Agent SDK) as a
localhost HTTP API: REST, Server-Sent-Events streaming, and WebSocket. Built
for SAMURAI and other local applications that want to talk to Claude Code over
HTTP while preserving sessions, streaming output, and agent (tool) behavior.
"""

__version__ = "1.0.0"

"""API-key authentication (pure logic, no web framework imports).

A single shared bearer secret (``API_KEY``). Empty key disables auth entirely
— intended only for localhost development. The returned *owner* id is a stable,
non-reversible short hash of the presented key, used to scope sessions/jobs so
the design extends cleanly to multiple keys later.
"""

from __future__ import annotations

import hashlib
import hmac


class AuthError(Exception):
    """Raised when authentication fails."""


class ApiKeyAuth:
    def __init__(self, api_key: str) -> None:
        self._key = api_key or ""
        self.enabled = bool(self._key)

    def owner_id(self, token: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return "key_" + digest[:12]

    def authenticate(
        self, authorization: str | None, x_api_key: str | None
    ) -> str:
        """Return an owner id, or raise AuthError. ``anonymous`` when disabled."""
        if not self.enabled:
            return "anonymous"
        token = None
        if authorization:
            parts = authorization.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
        if token is None and x_api_key:
            token = x_api_key.strip()
        if not token:
            raise AuthError("missing API key")
        # Constant-time comparison.
        if not hmac.compare_digest(token, self._key):
            raise AuthError("invalid API key")
        return self.owner_id(token)

"""Audit logging — append-only JSONL record of security-relevant actions.

Distinct from request logging (which is operational). The audit trail records
*who did what*: job submissions, session deletes, auth failures. One JSON object
per line, suitable for ingestion into a SIEM.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..logging_config import get_logger

log = get_logger("gateway.audit")


class AuditLogger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        action: str,
        *,
        owner: str | None = None,
        client: str | None = None,
        outcome: str = "ok",
        **details: Any,
    ) -> None:
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "owner": owner,
            "client": client,
            "outcome": outcome,
            "details": details,
        }
        line = json.dumps(entry, default=str)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # pragma: no cover
            log.warning("audit write failed: %s", e)
        log.info("audit %s owner=%s outcome=%s", action, owner, outcome)

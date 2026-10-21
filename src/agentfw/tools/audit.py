"""Append-only JSONL audit log of every tool call, denial and command a role run makes.

One directory per run: <AGENTFW_HOME>/logs/<project>/<run_id>/ holds audit.jsonl
plus the full output of each command. The run_id is stored in every handoff
the run writes, so a document can be traced back to the calls behind it.
"""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..resolver import Resolved, agentfw_home

MAX_LOGGED_STRING = 300
# Argument values that are logged by size only, never content.
CONTENT_KEYS = {"content", "body", "document"}


def new_run_id() -> str:
    return f"run-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class AuditLog:
    def __init__(self, resolved: Resolved, run_id: str | None = None, root: Path | None = None):
        self.run_id = run_id or new_run_id()
        self.dir = (root or agentfw_home() / "logs") / resolved.project / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "audit.jsonl"
        self._base = {"run_id": self.run_id, "project": resolved.project, "role": resolved.role, "task": resolved.task}
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **self._base, "event": event, **fields}
        self.events.append(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def output_file(self, name: str) -> Path:
        """A fresh file in the run directory for one command's full output."""
        self._seq += 1
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return self.dir / f"{self._seq:03d}-{safe}.log"


def summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as logged: long strings truncated, file contents reduced to their size."""
    out = {}
    for key, value in args.items():
        if key in CONTENT_KEYS:
            out[key] = f"<{len(json.dumps(value, default=str))} chars>"
        elif isinstance(value, str) and len(value) > MAX_LOGGED_STRING:
            out[key] = value[:MAX_LOGGED_STRING] + f"... <{len(value)} chars>"
        else:
            out[key] = value
    return out

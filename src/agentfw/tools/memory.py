"""memory.read / memory.write: short notes a role keeps for later tasks.

Stored as JSONL under <AGENTFW_HOME>/memory/<project>/:
  scope: project  ->  <role>.jsonl               shared across the project's tasks
  scope: task     ->  <role>/<task>.jsonl        forgotten when the task ends

memory.read with a query ranks notes by meaning (same local embedding model as
the knowledge index); without one it returns the newest notes. Notes that look
like they contain credentials are refused, since memory is plain text on disk.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ..resolver import agentfw_home
from ..retrieval.embeddings import cosine, get_embeddings
from .guard import ToolInputError

MAX_RESULTS = 10
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(AKIA|ASIA)[0-9A-Z]{16}\b"                      # AWS access key id
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"                   # GitHub tokens
    r"|\bsk-[A-Za-z0-9_-]{20,}\b"                        # API keys in the sk- style
    r"|(?i:\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,})"
)


def store_path(resolved) -> Path:
    base = agentfw_home() / "memory" / resolved.project
    if resolved.memory.get("scope", "task") == "project":
        return base / f"{resolved.role}.jsonl"
    return base / resolved.role / f"{resolved.task or 'no-task'}.jsonl"


def write(ctx, note: str, tags: list[str] | None = None) -> str:
    note = note.strip()
    if not note:
        raise ToolInputError("note is empty")
    if SECRET.search(note):
        raise ToolInputError("the note looks like it contains a credential; memory is stored as plain text, so leave it out")
    path = store_path(ctx.resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_id": ctx.audit.run_id,
        "task": ctx.resolved.task,
        "note": note,
        "tags": tags or [],
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return f"Saved to {ctx.resolved.role} memory ({ctx.resolved.memory.get('scope', 'task')} scope)."


def read(ctx, query: str | None = None) -> str:
    entries = load(ctx.resolved)
    if not entries:
        return "No notes yet."
    if query:
        embeddings = get_embeddings()
        vectors = embeddings.embed_documents([_text(e) for e in entries])
        q = embeddings.embed_query(query)
        ranked = sorted(zip(entries, vectors), key=lambda ev: cosine(q, ev[1]), reverse=True)
        picked = [e for e, _ in ranked[:MAX_RESULTS]]
        header = f"Notes most related to {query!r}:"
    else:
        picked = list(reversed(entries))[:MAX_RESULTS]
        header = "Most recent notes:"
    return header + "\n" + "\n".join(_format(e) for e in picked)


def load(resolved) -> list[dict]:
    path = store_path(resolved)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _text(e: dict) -> str:
    return e["note"] + (" " + " ".join(e["tags"]) if e.get("tags") else "")


def _format(e: dict) -> str:
    where = f" {e['task']}" if e.get("task") else ""
    tags = f"  [{', '.join(e['tags'])}]" if e.get("tags") else ""
    return f"- ({e['ts'][:10]}{where}) {e['note']}{tags}"

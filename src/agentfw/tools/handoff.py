"""handoff.read / handoff.list / handoff.write."""
from __future__ import annotations

from ..handoffs import HandoffError, dump
from .guard import ToolInputError


def _store(ctx):
    if ctx.handoffs is None:
        raise ToolInputError("no handoff store is configured for this run")
    return ctx.handoffs


def read(ctx, id: str) -> str:   # noqa: A002 - tool argument name
    try:
        return dump(_store(ctx).get(id))
    except HandoffError as e:
        raise ToolInputError(str(e)) from None


def list(ctx, type: str | None = None, status: str | None = None) -> str:   # noqa: A001,A002
    docs = _store(ctx).list(type=type, status=status)
    if not docs:
        return "No matching handoff documents."
    return "\n".join(f"{d['id']}  [{d['status']}]  {d['title']}" for d in docs)


def write(ctx, document: dict) -> str:
    try:
        doc = _store(ctx).write(document, resolved=ctx.resolved, run_id=ctx.audit.run_id)
    except HandoffError as e:
        raise ToolInputError(str(e)) from None
    ctx.audit.record("handoff_written", handoff=doc["id"], type=doc["type"], status=doc["status"])
    next_step = "It now waits for human approval." if doc["status"] == "pending_approval" else ""
    return f"Wrote {doc['id']} ({doc['type']}, status {doc['status']}). {next_step}".strip()

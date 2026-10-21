"""Turn a Resolved role into LangChain tools.

Three sources:
- registry tools (fs.read, git.commit, github.pr.create, ...), from tools/registry.yaml
- pack command tools (ios.build, mule.test, ...), run by the CommandRunner
- run_check, if the role has verification checks

Every tool is wrapped the same way: arguments are validated against the
tool's JSON Schema, the call runs, and the outcome (ok / denied / error) is
written to the audit log. Errors and denials are returned to the agent as
text instead of raised, so it can adjust and continue.
"""
from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool

from ..resolver import Resolved
from .audit import AuditLog, summarize_args
from .guard import Guard, PermissionDenied, ToolInputError
from .runner import CommandRunner

EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass
class ToolContext:
    """What a tool implementation gets: the resolved role plus the shared guard, runner, audit log,
    handoff store and human channel."""
    resolved: Resolved
    guard: Guard
    audit: AuditLog
    runner: CommandRunner
    handoffs: Any = None        # HandoffStore
    human: Any = None           # HumanChannel


@dataclass
class Toolset:
    context: ToolContext
    tools: list[StructuredTool] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)       # tool name -> "registry" | "pack:<id>" | "checks"
    ids: dict[str, str] = field(default_factory=dict)           # tool name -> framework id (fs.read)
    unavailable: dict[str, str] = field(default_factory=dict)   # framework id -> reason

    def get(self, name_or_id: str) -> StructuredTool:
        name = tool_name(name_or_id)
        for t in self.tools:
            if t.name == name:
                return t
        reason = self.unavailable.get(name_or_id)
        raise KeyError(f"tool {name_or_id!r} " + (f"is unavailable: {reason}" if reason else "isn't available to this role"))


def tool_name(tool_id: str) -> str:
    """LLM APIs only accept [a-zA-Z0-9_-] in tool names, so 'fs.read' becomes 'fs_read'."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_id)


def build_toolset(
    resolved: Resolved,
    *,
    audit: AuditLog | None = None,
    run_id: str | None = None,
    check_requirements: bool = True,
    handoffs: Any = None,
    human: Any = None,
) -> Toolset:
    """Build every tool the role can use.

    With check_requirements, pack commands and checks refuse to run while the
    host requirements aren't met (the agent gets an error explaining what's
    missing); other tools work regardless. The pipeline should also call
    ensure_requirements() up front, to fail before spending any tokens.
    """
    audit = audit or AuditLog(resolved, run_id)
    guard = Guard(resolved)
    runner = CommandRunner(resolved, guard, audit, enforce_requirements=check_requirements)
    ctx = ToolContext(resolved, guard, audit, runner, handoffs, human)
    ts = Toolset(ctx)

    for entry in resolved.tools:
        impl = entry["impl"]
        if "mcp_server" in impl:
            ts.unavailable[entry["id"]] = f"MCP server {impl['mcp_server']!r} isn't connected yet (M6)"
            continue
        fn = _load_builtin(impl["builtin"])
        if fn is None:
            ts.unavailable[entry["id"]] = f"{impl['builtin']} isn't implemented yet"
            continue
        _add(ts, entry["id"], entry["description"], entry.get("args") or EMPTY_SCHEMA, "registry",
             lambda _fn=fn, **kw: _fn(ctx, **kw))

    for tool in resolved.pack_tools:
        _add(ts, tool.id, tool.description, _pack_args_schema(tool.args), f"pack:{tool.pack}",
             lambda _tool=tool, **kw: ctx.runner.run_tool(_tool, kw).format())

    if resolved.verification:
        checks = {c.id: c for c in resolved.verification}
        listing = "; ".join(f"{c.id}{' (blocking)' if c.blocking else ''}" for c in resolved.verification)
        _add(
            ts, "run_check",
            f"Run one of this task's verification checks and get pass/fail with the relevant output. Checks: {listing}.",
            {"type": "object", "required": ["check_id"], "additionalProperties": False,
             "properties": {"check_id": {"type": "string", "enum": list(checks)}}},
            "checks",
            lambda check_id: ctx.runner.run_check(checks[check_id]).format(),
        )
    return ts


def _add(ts: Toolset, tool_id: str, description: str, schema: dict, source: str, fn: Callable[..., str]) -> None:
    name = tool_name(tool_id)
    if name in ts.sources:
        raise ValueError(f"two tools map to the name {name!r}")
    ts.tools.append(StructuredTool.from_function(
        func=_guarded(ts.context, tool_id, schema, fn), name=name, description=description, args_schema=schema,
    ))
    ts.sources[name] = source
    ts.ids[name] = tool_id


def _guarded(ctx: ToolContext, tool_id: str, schema: dict, fn: Callable[..., str]) -> Callable[..., str]:
    validator = Draft202012Validator(schema)

    def call(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}     # models often send explicit nulls
        logged = summarize_args(args)
        start = time.monotonic()

        def record(outcome: str, **extra: Any) -> None:
            ctx.audit.record("tool_call", tool=tool_id, args=logged, outcome=outcome,
                             duration_ms=round((time.monotonic() - start) * 1000), **extra)

        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
        if errors:
            message = "; ".join(f"{'/'.join(map(str, e.path)) or 'arguments'}: {e.message}" for e in errors)
            record("error", reason=message)
            return f"Error: invalid arguments: {message}"
        try:
            result = fn(**args)
        except PermissionDenied as e:
            record("denied", reason=str(e))
            return f"Denied: {e}"
        except ToolInputError as e:
            record("error", reason=str(e))
            return f"Error: {e}"
        except Exception as e:            # an unexpected failure must not kill the agent loop
            record("error", reason=f"{type(e).__name__}: {e}")
            return f"Error: {type(e).__name__}: {e}"
        record("ok")
        return result

    return call


def _pack_args_schema(specs: dict[str, dict]) -> dict:
    props = {}
    for name, spec in specs.items():
        prop = {"type": spec["type"], "description": spec["description"]}
        if "pattern" in spec:
            prop["pattern"] = spec["pattern"]
        props[name] = prop
    required = [name for name, spec in specs.items() if spec.get("required")]
    return {"type": "object", "properties": props, "additionalProperties": False, **({"required": required} if required else {})}


def _load_builtin(path: str) -> Callable | None:
    module_name, _, attr = path.partition(":")
    try:
        return getattr(importlib.import_module(module_name), attr)
    except (ModuleNotFoundError, AttributeError):
        return None

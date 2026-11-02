"""Run one role as a tool-calling agent: resolve → tools → create_agent → handoff documents.

The agent loop is LangChain's create_agent. Around it the runner adds:
- the combined prompt (role + pack instructions), plus the body formats of the
  documents this role produces, plus run context (date, project, inputs)
- input documents inline in the first message, so the agent doesn't spend
  calls fetching them
- limits from the role config, enforced by LimitsMiddleware: model calls
  (max_iterations), tokens (max_tokens_per_task) and wall-clock time (timeout_minutes)
- one reminder if the agent stops without writing the documents it was expected to
- audit events for the start, end, token usage and outputs
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware, ClearToolUsesEdit, ContextEditingMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware,
    hook_config,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .config import Workspace
from .handoffs import HandoffStore, dump
from .models import is_transient, make_fallbacks, make_model
from .resolver import Resolved, resolve
from .tools import build_toolset
from .tools.audit import AuditLog
from .tools.requirements import ensure_requirements

REMINDERS = 1

# Every tool result is re-sent on every later model call, so a long run's cost grows
# with the square of its steps. Past this many context tokens, older tool results are
# replaced by a note (the run's state keeps them; only what the model sees shrinks).
CONTEXT_TRIGGER_TOKENS = 30_000      # 15k with keep=3 made the PM re-read cleared files in a loop
KEEP_TOOL_RESULTS = 4
CLEARED_NOTE = "[older tool result cleared to save tokens; run the tool again if you need its exact text]"

# Share of the token budget (or all but this many model calls) after which the agent is
# told once to stop researching and write, so a run doesn't end with nothing written.
WRAP_UP_AT = 0.8
WRAP_UP_CALLS_LEFT = 3
WRAP_UP = ("You are close to this run's limit. Stop researching and write your output document(s) now "
           "with handoff_write, using what you have; record anything unresolved in the document.")


@dataclass
class RunResult:
    run_id: str
    role: str
    outputs: list[str]                       # handoff ids written in this run
    stop_reason: str                         # finished | model_call_limit | token_budget | timeout | no_output
    model_calls: int
    tool_calls: int
    tokens: dict[str, int]
    audit_path: str
    final_message: str
    questions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop_reason == "finished" and bool(self.outputs)


class LimitsMiddleware(AgentMiddleware):
    """Enforce the role's limits: model calls, tokens and wall-clock time.

    One instance per role run, so counts carry across the reminder turn. The
    reason for stopping is kept in .stopped for the run result.
    """

    def __init__(self, max_calls: int, max_tokens: int | None, timeout_minutes: int | None):
        super().__init__()
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.deadline = time.monotonic() + timeout_minutes * 60 if timeout_minutes else None
        self.calls = 0
        self.stopped: str | None = None
        self.warned = False

    def after_model(self, state, runtime) -> None:
        self.calls += 1

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        if self.calls >= self.max_calls:
            self.stopped = "model_call_limit"
            return {"jump_to": "end", "messages": [AIMessage(f"Stopped: reached the limit of {self.max_calls} model calls.")]}
        used = tokens_used(state["messages"])["total"]
        if self.max_tokens and used >= self.max_tokens:
            self.stopped = "token_budget"
            return {"jump_to": "end", "messages": [AIMessage(f"Stopped: token budget of {self.max_tokens} used up ({used}).")]}
        if self.deadline and time.monotonic() > self.deadline:
            self.stopped = "timeout"
            return {"jump_to": "end", "messages": [AIMessage("Stopped: the role's time limit was reached.")]}
        near = self.calls >= self.max_calls - WRAP_UP_CALLS_LEFT or (self.max_tokens and used >= self.max_tokens * WRAP_UP_AT)
        if near and not self.warned:
            self.warned = True
            return {"messages": [HumanMessage(WRAP_UP)]}
        return None


def run_role(
    ws: Workspace,
    project_id: str,
    role_id: str,
    *,
    inputs: Sequence[str] = (),
    task: dict | None = None,
    model: BaseChatModel | None = None,
    model_id: str | None = None,
    handoffs: HandoffStore | None = None,
    human: Any = None,
    expect: Sequence[str] | None = None,
    instructions: str | None = None,
    on_event: Callable[[str], None] | None = None,
) -> RunResult:
    """Run a role once. inputs are handoff ids given to the agent; expect lists document types it must write."""
    resolved = resolve(ws, project_id, role_id, task)
    if resolved.requirements and (resolved.pack_tools or resolved.verification):
        ensure_requirements(resolved)                 # fail before spending tokens
    store = handoffs or HandoffStore(ws, project_id)
    input_docs = [store.get(i) for i in inputs]
    llm = model or make_model(resolved.model, model_id)
    fallbacks = [] if model else make_fallbacks(resolved.model)

    audit = AuditLog(resolved)
    toolset = build_toolset(resolved, audit=audit, handoffs=store, human=human)
    limits = resolved.limits
    budget = LimitsMiddleware(limits["max_iterations"], limits.get("max_tokens_per_task"), limits.get("timeout_minutes"))
    middleware: list[AgentMiddleware] = [budget, ContextEditingMiddleware(edits=[ClearToolUsesEdit(
        trigger=CONTEXT_TRIGGER_TOKENS, keep=KEEP_TOOL_RESULTS, placeholder=CLEARED_NOTE,
        exclude_tools=("handoff_write", "human_ask"),         # rejections and human answers stay visible
    )])]
    if fallbacks:
        middleware.append(ModelFallbackMiddleware(*fallbacks))
    if not model:
        # Inside the fallback, so each model gets its own retries before the next one is tried.
        middleware.append(ModelRetryMiddleware(max_retries=4, retry_on=is_transient, on_failure="error",
                                               initial_delay=5.0, max_delay=60.0))

    agent = create_agent(llm, toolset.tools, system_prompt=system_prompt(ws, resolved, store), middleware=middleware)
    model_name = type(model).__name__ if model else f"{resolved.model['provider']}/{model_id or resolved.model['id']}"
    audit.record("agent_start", model=model_name, inputs=list(inputs), expect=list(expect or []),
                 unavailable_tools=sorted(toolset.unavailable))

    messages: list = [HumanMessage(first_message(input_docs, expect, instructions))]
    written_before = set(store.all())
    reminders = 0
    while True:
        messages = _stream(agent, messages, limits["max_iterations"], on_event)
        outputs = [i for i in store.all() if i not in written_before]
        missing = _missing(store, outputs, expect)
        if budget.stopped or not missing or reminders >= REMINDERS:
            break
        reminders += 1
        if on_event:
            on_event(f"reminding the agent to write: {', '.join(missing)}")
        messages.append(HumanMessage(
            f"You haven't written the required document(s): {', '.join(missing)}. "
            "Write them now with handoff_write, then stop."
        ))

    outputs = sorted(i for i in store.all() if i not in written_before)
    if budget.stopped:
        stop = budget.stopped
    elif _missing(store, outputs, expect) or not outputs:
        stop = "no_output"
    else:
        stop = "finished"
    tokens = tokens_used(messages)
    model_calls = budget.calls
    tool_calls = sum(1 for m in messages if isinstance(m, ToolMessage))
    final = next((m.content for m in reversed(messages) if isinstance(m, AIMessage) and m.content), "")
    final = final if isinstance(final, str) else json.dumps(final)
    audit.record("agent_end", stop_reason=stop, outputs=outputs, tokens=tokens, model_calls=model_calls, tool_calls=tool_calls)
    questions = [e["question"] for e in audit.events if e["event"] == "human_ask"]
    return RunResult(audit.run_id, role_id, outputs, stop, model_calls, tool_calls, tokens, str(audit.path), final, questions)


# ---- Prompt ------------------------------------------------------------------

def system_prompt(ws: Workspace, resolved: Resolved, store: HandoffStore) -> str:
    formats = []
    defs = ws.schemas["handoff"]["$defs"]
    for htype in resolved.produces:
        formats.append(f"### {htype}\n```json\n{json.dumps(_inline_refs(defs[htype], defs), indent=1)}\n```")
    return "\n\n".join([
        resolved.instructions,
        "---\n\n## Run context\n"
        f"- Today: {date.today().isoformat()}\n"
        f"- Project: {resolved.project} (root {resolved.project_root})\n"
        f"- Handoff documents live in {store.dir}; use handoff_read / handoff_list / handoff_write.\n"
        "- Tool results starting with 'Denied:' or 'Error:' mean the call didn't happen; adjust and retry, "
        "or explain why you can't.\n"
        "- When you've written your documents, reply with a short summary and stop.",
        "## Output formats\nThe `body` of each document you write must match its schema. "
        "Don't include id, status, created_by or created_at; the framework sets them.\n\n" + "\n\n".join(formats),
    ])


def first_message(input_docs: list[dict], expect: Sequence[str] | None, instructions: str | None) -> str:
    parts = []
    if instructions:
        parts.append(instructions)
    if input_docs:
        parts.append("Your input document(s):\n\n" + "\n\n".join(f"```yaml\n{dump(d)}```" for d in input_docs))
    if expect:
        parts.append(f"Write: {', '.join(expect)}. Use the input ids as parents.")
    return "\n\n".join(parts) or "Start."


def _inline_refs(schema: Any, defs: dict) -> Any:
    """Replace local $refs with their definitions so the model sees one self-contained schema."""
    if isinstance(schema, dict):
        ref = schema.get("$ref", "")
        if ref.startswith("#/$defs/"):
            return _inline_refs(defs[ref.split("/")[-1]], defs)
        if ref.startswith("role.schema.json#/$defs/artifactType"):
            return {"type": "string"}
        return {k: _inline_refs(v, defs) for k, v in schema.items() if k != "description" or len(str(v)) < 200}
    if isinstance(schema, list):
        return [_inline_refs(v, defs) for v in schema]
    return schema


# ---- Loop helpers ------------------------------------------------------------

def _stream(agent, messages: list, max_iterations: int, on_event) -> list:
    final_state = None
    # Each model call is 4 graph steps (before_model, model, after_model, tools), so the
    # role's own iteration limit, which stops cleanly, is always reached first.
    config = {"recursion_limit": max_iterations * 4 + 10}
    for mode, chunk in agent.stream({"messages": messages}, config=config, stream_mode=["updates", "values"]):
        if mode == "values":
            final_state = chunk
        elif on_event:
            for update in chunk.values():
                for m in (update or {}).get("messages", []) if isinstance(update, dict) else []:
                    _describe(m, on_event)
    return list(final_state["messages"]) if final_state else messages


def _describe(m, on_event) -> None:
    if isinstance(m, AIMessage):
        for call in m.tool_calls:
            args = json.dumps(call["args"])[:160]
            on_event(f"→ {call['name']}({args})")
        if m.content and not m.tool_calls:
            text = m.content if isinstance(m.content, str) else json.dumps(m.content)
            on_event(f"agent: {text[:300]}")
    elif isinstance(m, ToolMessage):
        text = str(m.content).splitlines()[0][:160] if m.content else ""
        on_event(f"  ← {text}")


def _missing(store: HandoffStore, outputs: list[str], expect: Sequence[str] | None) -> list[str]:
    written_types = {store.get(i)["type"] for i in outputs}
    return [t for t in (expect or []) if t not in written_types]


def tokens_used(messages) -> dict[str, int]:
    total = {"input": 0, "output": 0, "total": 0}
    for m in messages:
        usage = getattr(m, "usage_metadata", None)
        if usage:
            total["input"] += usage.get("input_tokens", 0)
            total["output"] += usage.get("output_tokens", 0)
            total["total"] += usage.get("total_tokens", 0)
    return total

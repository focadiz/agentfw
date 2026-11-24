"""The pipeline: roles run in order as a LangGraph graph that pauses at human approval gates.

    idea → PM: spec ⏸ → PM: task_list → Architect: design ⏸ → Architect: tasks → ready for development

(Developer ⇄ QA comes in M6.) The handoff store is the source of truth, not the graph:
- Each step first looks for the document it would write (a document of its type whose
  parents include its first input). If one exists, the role doesn't run again, so a
  pipeline that crashed, or was started over, picks up where the documents are.
- A gate reads the document's status. Pending → the graph pauses with interrupt().
  `fw approve` / `fw revise` / `fw reject` record the decision on the document and
  resume the graph; approved goes on, changes requested sends the human's comment
  back to the role to revise the same document, rejected ends the pipeline.
- Checkpoints live in <AGENTFW_HOME>/pipelines/<project>.sqlite, one thread per idea
  and handoffs folder, so a pause survives between commands.
"""
from __future__ import annotations

import hashlib
import operator
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .agent import run_role
from .config import Workspace
from .handoffs import HandoffStore
from .resolver import agentfw_home
from .tools.human import DeferredHuman


@dataclass(frozen=True)
class Step:
    produces: str
    role: str
    inputs: tuple[str, ...]        # document types the role gets; its output's parents include the first one

    @property
    def node(self) -> str:
        return f"{self.role}_{self.produces}"


STEPS = (
    Step("spec", "pm", ("idea",)),
    Step("task_list", "pm", ("spec",)),
    Step("design", "architect", ("spec", "task_list")),
    Step("task", "architect", ("design", "spec", "task_list")),
)
DONE = "ready_for_development"


class State(TypedDict, total=False):
    idea: str
    docs: dict[str, list[str]]                       # document type -> ids the pipeline is working with
    runs: Annotated[list[dict], operator.add]        # one summary per role run
    outcome: str                                     # ready_for_development | rejected | failed
    detail: str


@dataclass
class Status:
    idea: str
    state: str                     # not_started | running | waiting | ready_for_development | rejected | failed
    waiting_on: dict | None = None
    docs: dict[str, list[str]] = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)
    detail: str = ""


class PipelineError(Exception):
    pass


class Pipeline:
    def __init__(self, ws: Workspace, project_id: str, store: HandoffStore | None = None, *,
                 models: Mapping[str, BaseChatModel] | None = None, model_id: str | None = None,
                 on_event: Callable[[str], None] | None = None, checkpointer: Any = None):
        self.ws = ws
        self.project_id = project_id
        self.store = store or HandoffStore(ws, project_id)
        self.models = dict(models or {})               # role -> model, for tests
        self.model_id = model_id
        self.say = on_event or (lambda line: None)
        self.checkpointer = checkpointer or _sqlite_saver(project_id)
        self.graph = self._build()

    # ---- Public ------------------------------------------------------------------

    def run(self, idea_id: str) -> Status:
        """Start the pipeline for an idea, or continue it from where it stopped."""
        idea = self.store.get(idea_id)
        if idea["type"] != "idea":
            raise PipelineError(f"{idea_id} is a {idea['type']}, not an idea")
        if idea["status"] != "approved":
            raise PipelineError(f"{idea_id} is {idea['status']}; only approved ideas start the pipeline")
        config = self._config(idea_id)
        snap = self.graph.get_state(config)
        if snap.next and snap.interrupts:
            start: Any = Command(resume=True)          # the gate re-reads the document's status
        elif snap.next:
            start = None                               # stopped by an error: continue from the last checkpoint
        else:
            start = {"idea": idea_id, "docs": {"idea": [idea_id]}, "runs": [], "outcome": "", "detail": ""}
        self.graph.invoke(start, config)
        return self.status(idea_id)

    def status(self, idea_id: str) -> Status:
        snap = self.graph.get_state(self._config(idea_id))
        values = snap.values or {}
        if not values:
            return Status(idea_id, "not_started")
        waiting = snap.interrupts[0].value if snap.interrupts else None
        state = "waiting" if waiting else (values.get("outcome") or "running")
        return Status(idea_id, state, waiting, values.get("docs", {}), values.get("runs", []), values.get("detail", ""))

    def idea_of(self, handoff_id: str) -> str:
        """The idea a document descends from, following parents."""
        todo, seen = [handoff_id], set()
        while todo:
            i = todo.pop()
            if i in seen:
                continue
            seen.add(i)
            doc = self.store.get(i)
            if doc["type"] == "idea":
                return i
            todo.extend(doc.get("parents", []))
        raise PipelineError(f"{handoff_id} doesn't descend from an idea")

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()

    # ---- Graph -------------------------------------------------------------------

    def _build(self):
        graph = StateGraph(State)
        nodes = [s.node for s in STEPS] + ["done"]
        for n, step in enumerate(STEPS):
            after = nodes[n + 1]
            if self._gated(step):
                gate = f"approve_{step.produces}"
                graph.add_node(step.node, self._produce(step, gate), destinations=(gate, END))
                graph.add_node(gate, self._gate(step, after), destinations=(after, step.node, END))
            else:
                graph.add_node(step.node, self._produce(step, after), destinations=(after, END))
        graph.add_node("done", lambda state: {"outcome": DONE, "detail": "tasks are ready for development"})
        graph.add_edge(START, STEPS[0].node)
        graph.add_edge("done", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _gated(self, step: Step) -> bool:
        gates = self.ws.roles[step.role].data.get("gates", [])
        return any(g["artifact"] == step.produces and g["when"] == "before_handoff" for g in gates)

    def _produce(self, step: Step, after: str):
        def node(state: State) -> Command:
            docs = state["docs"]
            parent = docs[step.inputs[0]][-1]
            existing = self._outputs(step.produces, parent)
            to_revise = [d for d in existing if decision(d) == "changes_requested"]
            if existing and not to_revise:
                ids = [d["id"] for d in existing]
                self.say(f"{step.node}: using {', '.join(ids)}")
                return Command(goto=after, update={"docs": {**docs, step.produces: ids}})

            inputs = [i for t in step.inputs for i in docs.get(t, [])]
            instructions = None
            if to_revise:
                doc = to_revise[0]
                inputs.append(doc["id"])
                instructions = (f"At the approval gate, {doc['approval']['by']} asked for changes to {doc['id']}:\n\n"
                                f"> {doc['approval'].get('comment', '')}\n\n"
                                f"Revise it: call handoff_write with \"id\": \"{doc['id']}\" and the whole corrected "
                                "document. The document already reflects your earlier research: only look up what "
                                "these changes need. If the correction should also apply to later documents, save it "
                                "with memory.write.")
            self.say(f"{step.node}: running {step.role}" + (f" to revise {to_revise[0]['id']}" if to_revise else ""))
            result = run_role(
                self.ws, self.project_id, step.role, inputs=inputs, model=self.models.get(step.role),
                model_id=self.model_id, handoffs=self.store, human=DeferredHuman(), expect=[step.produces],
                instructions=instructions, on_event=lambda line: self.say(f"  [{step.role}] {line}"),
            )
            run = {"step": step.node, "run_id": result.run_id, "stop_reason": result.stop_reason,
                   "outputs": result.outputs, "model_calls": result.model_calls,
                   "tokens": result.tokens.get("total", 0), "questions": result.questions}
            ids = [i for i in result.outputs if self.store.get(i)["type"] == step.produces]
            if not result.ok or not ids:
                return Command(goto=END, update={"runs": [run], "outcome": "failed",
                                                 "detail": f"{step.node} stopped: {result.stop_reason}"})
            return Command(goto=after, update={"runs": [run], "docs": {**docs, step.produces: ids}})
        return node

    def _gate(self, step: Step, after: str):
        def node(state: State) -> Command:
            handoff_id = state["docs"][step.produces][-1]
            while True:
                doc = self.store.get(handoff_id)
                d = decision(doc)
                if d == "approved":
                    self.say(f"gate: {handoff_id} approved by {doc.get('approval', {}).get('by', '?')}")
                    return Command(goto=after)
                if d == "rejected":
                    return Command(goto=END, update={"outcome": "rejected",
                                                     "detail": f"{handoff_id} was rejected: "
                                                               f"{doc.get('approval', {}).get('comment', '')}"})
                if d == "changes_requested":
                    self.say(f"gate: changes requested on {handoff_id}")
                    return Command(goto=step.node)
                self.say(f"gate: waiting for approval of {handoff_id}")
                # Paused here until `fw approve|revise|reject`. On resume the node starts
                # over, so the document is read again rather than trusting the resume value.
                interrupt({"handoff": handoff_id, "type": step.produces, "title": doc["title"],
                           "author": doc["created_by"]["actor"], "path": str(self.store.path_of(handoff_id))})
        return node

    def _outputs(self, htype: str, parent: str) -> list[dict]:
        return [d for d in self.store.list(type=htype)
                if parent in d.get("parents", []) and d["status"] not in ("superseded", "rejected")]

    def _config(self, idea_id: str) -> dict:
        where = hashlib.sha1(str(self.store.dir.resolve()).encode()).hexdigest()[:8]
        return {"configurable": {"thread_id": f"{idea_id}@{where}"}}


def decision(doc: Mapping[str, Any]) -> str:
    """approved | rejected | changes_requested | pending, from a document's status and approval."""
    if doc["status"] in ("approved", "done"):
        return "approved"
    if doc["status"] == "rejected":
        return "rejected"
    if doc.get("approval", {}).get("decision") == "changes_requested" and doc["status"] == "draft":
        return "changes_requested"
    return "pending"


def _sqlite_saver(project_id: str):
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = agentfw_home() / "pipelines" / f"{project_id}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


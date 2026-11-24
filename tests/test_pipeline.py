"""The pipeline graph: steps, approval gates, revisions and resuming, with scripted models."""
import copy

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agentfw.handoffs import HandoffError, HandoffStore
from agentfw.pipeline import Pipeline, PipelineError
from agentfw.testing import ScriptedModel, tool_call
from agentfw.tools import rag


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTFW_EMBEDDINGS", "hashing")
    rag.reset_cache()


@pytest.fixture
def example(ws):
    return {i: copy.deepcopy(d.data) for i, d in ws.project_handoffs("ridexpress").items()}


@pytest.fixture
def store(ws, tmp_path, example):
    s = HandoffStore(ws, "ridexpress", tmp_path / "handoffs")
    s.put(example["idea-001"])
    return s


def write(example, handoff_id, **overrides):
    """A handoff_write call that reproduces an example document (the store gives it the same id)."""
    d = example[handoff_id]
    doc = {"type": d["type"], "title": d["title"], "parents": d["parents"], "body": copy.deepcopy(d["body"])}
    doc.update(overrides)
    return tool_call("handoff_write", document=doc)


STOP = AIMessage("Written.")      # ends one role run; the next run continues the same script


def pipeline(ws, store, pm=(), architect=(), **kw):
    models = {"pm": ScriptedModel(script=list(pm)), "architect": ScriptedModel(script=list(architect))}
    kw.setdefault("checkpointer", InMemorySaver())
    return Pipeline(ws, "ridexpress", store, models=models, **kw)


def test_runs_to_each_gate_and_on_to_ready_for_development(ws, store, example):
    events = []
    p = pipeline(ws, store,
                 pm=[write(example, "spec-001"), STOP, write(example, "task_list-001"), STOP],
                 architect=[write(example, "adr-001"), write(example, "design-001"), STOP,
                            write(example, "task-001"), write(example, "task-002"), STOP],
                 on_event=events.append)

    s = p.run("idea-001")
    assert s.state == "waiting" and s.waiting_on["handoff"] == "spec-001"
    assert store.get("spec-001")["status"] == "pending_approval"
    assert "gate: waiting for approval of spec-001" in events
    assert p.run("idea-001").waiting_on["handoff"] == "spec-001"         # nothing moves until a human decides

    store.decide("spec-001", "approved", by="focadiz")
    s = p.run("idea-001")
    assert s.state == "waiting" and s.waiting_on["handoff"] == "design-001"
    assert s.docs["task_list"] == ["task_list-001"]

    store.decide("design-001", "approved", by="focadiz", comment="looks right")
    s = p.run("idea-001")
    assert s.state == "ready_for_development"
    assert s.docs["task"] == ["task-001", "task-002"]
    assert [r["step"] for r in s.runs] == ["pm_spec", "pm_task_list", "architect_design", "architect_task"]
    assert store.get("design-001")["approval"]["comment"] == "looks right"


def test_changes_requested_sends_the_comment_back_and_the_author_revises(ws, store, example):
    revised = write(example, "spec-001", id="spec-001", title="UC-1 User Sign Up (v2)")
    p = pipeline(ws, store, pm=[write(example, "spec-001"), STOP, revised, STOP])
    p.run("idea-001")
    store.decide("spec-001", "changes_requested", by="focadiz", comment="Drop REQ-5; network loss is a later slice.")
    assert store.get("spec-001")["status"] == "draft"

    s = p.run("idea-001")
    assert s.state == "waiting" and s.waiting_on["handoff"] == "spec-001"
    doc = store.get("spec-001")
    assert doc["title"] == "UC-1 User Sign Up (v2)" and doc["status"] == "pending_approval" and "approval" not in doc
    first_message = p.models["pm"].seen[-1][1]
    assert isinstance(first_message, HumanMessage) and "Drop REQ-5" in first_message.content
    assert [(r["step"], r["outputs"]) for r in s.runs] == [("pm_spec", ["spec-001"]), ("pm_spec", ["spec-001"])]


def test_a_revision_written_as_a_new_document_supersedes_the_old_one(ws, store, example):
    p = pipeline(ws, store, pm=[write(example, "spec-001"), STOP, write(example, "spec-001"), STOP])  # no id: new doc
    p.run("idea-001")
    store.decide("spec-001", "changes_requested", by="focadiz", comment="Tighten REQ-2.")
    s = p.run("idea-001")
    assert s.waiting_on["handoff"] == "spec-002"
    assert store.get("spec-001")["status"] == "superseded"
    fresh = pipeline(ws, store)                                   # starting over doesn't revise spec-001 again
    assert fresh.run("idea-001").waiting_on["handoff"] == "spec-002" and fresh.models["pm"].seen == []


def test_rejected_ends_the_pipeline(ws, store, example):
    p = pipeline(ws, store, pm=[write(example, "spec-001")])
    p.run("idea-001")
    store.decide("spec-001", "rejected", by="focadiz", comment="Not this quarter.")
    s = p.run("idea-001")
    assert s.state == "rejected" and "Not this quarter." in s.detail


def test_a_role_that_writes_nothing_fails_the_pipeline(ws, store):
    s = pipeline(ws, store).run("idea-001")          # the scripted PM only says "Done."
    assert s.state == "failed" and "pm_spec stopped: no_output" in s.detail


def test_existing_documents_are_reused_instead_of_rerunning_roles(ws, store, example):
    for i in ("spec-001", "task_list-001"):
        store.put(example[i])                          # approved already, e.g. written before the pipeline existed
    p = pipeline(ws, store, architect=[write(example, "adr-001"), write(example, "design-001")])
    s = p.run("idea-001")
    assert s.waiting_on["handoff"] == "design-001"
    assert [r["step"] for r in s.runs] == ["architect_design"]
    assert p.models["pm"].seen == []


def test_a_pause_survives_a_new_process(ws, store, example):
    """The default checkpointer is SQLite under AGENTFW_HOME, so a later command resumes the same thread."""
    first = pipeline(ws, store, pm=[write(example, "spec-001")], checkpointer=None)
    first.run("idea-001")
    store.decide("spec-001", "approved", by="focadiz")

    second = pipeline(ws, store, pm=[write(example, "task_list-001")], checkpointer=None)
    assert second.status("idea-001").waiting_on["handoff"] == "spec-001"
    s = second.run("idea-001")
    assert s.docs["task_list"] == ["task_list-001"]
    assert [r["step"] for r in s.runs] == ["pm_spec", "pm_task_list", "architect_design"]


def test_decide_checks_status_and_comment(store, example, ws):
    store.put({**example["spec-001"], "status": "pending_approval"})
    with pytest.raises(HandoffError, match="say why"):
        store.decide("spec-001", "changes_requested", by="focadiz")
    store.decide("spec-001", "approved", by="focadiz")
    with pytest.raises(HandoffError, match="is approved, not waiting"):
        store.decide("spec-001", "approved", by="focadiz")


def test_a_document_that_no_longer_validates_can_be_sent_back_but_not_approved(store, example):
    bad = copy.deepcopy(example["design-001"])
    bad["body"]["components"][0]["repo"] = "user-sign-up-process-api"    # valid before the repo check existed
    for i in ("spec-001", "task_list-001", "adr-001"):
        store.put(example[i])
    store._save({**bad, "status": "pending_approval"})
    with pytest.raises(HandoffError, match="can't approve design-001"):
        store.decide("design-001", "approved", by="focadiz")
    assert store.decide("design-001", "changes_requested", by="focadiz", comment="fix the repo")["status"] == "draft"


def test_only_approved_ideas_start_and_documents_trace_back_to_their_idea(ws, store, example):
    p = pipeline(ws, store)
    store.put({**example["idea-001"], "id": "idea-002", "status": "draft"})
    with pytest.raises(PipelineError, match="only approved ideas"):
        p.run("idea-002")
    store.put(example["spec-001"])
    store.put(example["task_list-001"])
    assert p.idea_of("task_list-001") == "idea-001"


def test_cli_decisions_check_the_approver_and_need_a_comment(store, example, capsys):
    from agentfw.cli import main
    store.put({**example["spec-001"], "status": "pending_approval"})
    d = ["--handoffs-dir", str(store.dir)]
    assert main(["approve", "ridexpress", "spec-001", "--by", "someone", "--no-resume", *d]) == 2
    assert "isn't an approver" in capsys.readouterr().err
    with pytest.raises(SystemExit):                                # -m is required for revise and reject
        main(["revise", "ridexpress", "spec-001", *d])
    assert main(["approve", "ridexpress", "spec-001", "--no-resume", *d]) == 0   # the project's only approver
    out = capsys.readouterr().out
    assert "spec-001: approved by focadiz" in out and "fw pipeline ridexpress idea-001 --handoffs-dir" in out
    assert store.get("spec-001")["approval"]["by"] == "focadiz"
    assert main(["pipeline", "ridexpress", "idea-001", "--status", *d]) == 1
    assert "pipeline idea-001: not_started" in capsys.readouterr().out

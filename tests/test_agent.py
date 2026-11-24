"""Handoff store, handoff/human tools, the model factory and the role runner (with a scripted model)."""
import copy

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentfw.agent import CLEARED_NOTE, WRAP_UP, run_role, system_prompt
from agentfw.config import load_workspace
from agentfw.handoffs import HandoffError, HandoffStore
from agentfw.models import ModelConfigError, make_model
from agentfw.resolver import resolve
from agentfw.testing import ScriptedModel, tool_call
from agentfw.tools import rag
from agentfw.tools.human import ScriptedHuman


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


def spec_body(example):
    return copy.deepcopy(example["spec-001"]["body"])


def write_spec(example, **overrides):
    doc = {"type": "spec", "title": "UC-1 User Sign Up", "parents": ["idea-001"], "body": spec_body(example)}
    doc.update(overrides)
    return tool_call("handoff_write", document=doc)


# ---- Store -------------------------------------------------------------------

def test_store_fills_the_envelope_and_status_from_gates(ws, store, example):
    pm = resolve(ws, "ridexpress", "pm")
    doc = store.write({"type": "spec", "title": "t", "parents": ["idea-001"], "body": spec_body(example)}, resolved=pm, run_id="run-x")
    assert doc["id"] == "spec-001" and doc["status"] == "pending_approval"         # PM has a gate on spec
    assert doc["created_by"] == {"actor": "pm", "run_id": "run-x"}
    assert store.path_of("spec-001").is_file()
    assert store.next_id("spec") == "spec-002"


def test_store_rejects_wrong_type_bad_body_and_missing_parents(ws, store, example):
    pm = resolve(ws, "ridexpress", "pm")
    with pytest.raises(HandoffError, match="can't write 'design'"):
        store.write({"type": "design", "title": "t", "body": {}}, resolved=pm, run_id="r")
    body = spec_body(example)
    del body["requirements"][0]["acceptance_criteria"]
    with pytest.raises(HandoffError, match="'acceptance_criteria' is a required property"):
        store.write({"type": "spec", "title": "t", "parents": ["idea-001"], "body": body}, resolved=pm, run_id="r")
    with pytest.raises(HandoffError, match="parent 'idea-999' not found"):
        store.write({"type": "spec", "title": "t", "parents": ["idea-999"], "body": spec_body(example)}, resolved=pm, run_id="r")
    dup = spec_body(example)
    dup["requirements"][1]["id"] = "REQ-1"
    with pytest.raises(HandoffError, match="REQ-1 is used twice"):
        store.write({"type": "spec", "title": "t", "parents": ["idea-001"], "body": dup}, resolved=pm, run_id="r")
    assert store.list(type="spec") == []


def test_revising_is_limited_to_the_authors_unapproved_documents(ws, store, example):
    pm = resolve(ws, "ridexpress", "pm")
    first = store.write({"type": "spec", "title": "v1", "parents": ["idea-001"], "body": spec_body(example)}, resolved=pm, run_id="r")
    second = store.write({"id": first["id"], "type": "spec", "title": "v2", "parents": ["idea-001"], "body": spec_body(example)},
                         resolved=pm, run_id="r")
    assert second["id"] == "spec-001" and store.get("spec-001")["title"] == "v2"
    approved = dict(store.get("spec-001"), status="approved")
    store.put(approved)
    with pytest.raises(HandoffError, match="only before approval"):
        store.write({"id": "spec-001", "type": "spec", "title": "v3", "parents": ["idea-001"], "body": spec_body(example)},
                    resolved=pm, run_id="r")


def test_task_list_must_reference_real_requirements(ws, store, example):
    pm = resolve(ws, "ridexpress", "pm")
    store.put(dict(example["spec-001"], status="approved"))
    stories = copy.deepcopy(example["task_list-001"]["body"])
    stories["stories"][0]["requirement_ids"] = ["REQ-1", "REQ-42"]
    with pytest.raises(HandoffError, match="REQ-42, which isn't in spec-001"):
        store.write({"type": "task_list", "title": "t", "parents": ["spec-001"], "body": stories}, resolved=pm, run_id="r")


# ---- Prompt ------------------------------------------------------------------

def test_system_prompt_includes_self_contained_output_formats(ws, store):
    prompt = system_prompt(ws, resolve(ws, "ridexpress", "pm"), store)
    assert "# Role: Product Manager" in prompt and "## Output formats" in prompt
    assert "### spec" in prompt and "### task_list" in prompt and "acceptance_criteria" in prompt
    assert "$ref" not in prompt and "^REQ-[0-9]+$" in prompt


# ---- Model factory ---------------------------------------------------------------

def test_model_factory_errors_are_actionable(monkeypatch):
    with pytest.raises(ModelConfigError, match="no model chosen"):
        make_model({"provider": "openrouter", "id": "<model-id>"})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ModelConfigError, match="OPENROUTER_API_KEY isn't set"):
        make_model({"provider": "openrouter", "id": "some/model"})


def test_openrouter_model_is_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    m = make_model({"provider": "openrouter", "id": "some/model", "temperature": 0.3, "max_output_tokens": 999})
    assert m.model_name == "some/model" and m.openai_api_base == "https://openrouter.ai/api/v1"
    assert m.temperature == 0.3 and m.max_tokens == 999


# ---- Runner ------------------------------------------------------------------

def test_pm_run_end_to_end(ws, store, example):
    def fix_after_rejection(messages):
        last = messages[-1]
        assert isinstance(last, ToolMessage) and "document rejected" in last.content
        assert "acceptance_criteria" in last.content
        return write_spec(example)

    bad = spec_body(example)
    del bad["requirements"][0]["acceptance_criteria"]
    model = ScriptedModel(script=[
        tool_call("rag_search", query="sign up edge conditions"),
        tool_call("human_ask", question="Which API names are canonical?"),
        tool_call("handoff_write", document={"type": "spec", "title": "x", "parents": ["idea-001"], "body": bad}),
        fix_after_rejection,
        AIMessage("Wrote spec-001 for UC-1."),
    ])
    events = []
    result = run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store,
                      human=ScriptedHuman([]), expect=["spec"], on_event=events.append)

    assert result.ok and result.stop_reason == "finished"
    assert result.outputs == ["spec-001"] and result.model_calls == 5 and result.tool_calls == 4
    assert result.questions == ["Which API names are canonical?"]
    spec = store.get("spec-001")
    assert spec["status"] == "pending_approval" and spec["created_by"]["run_id"] == result.run_id
    assert spec["body"] == spec_body(example)
    # The agent got the idea inline and the role prompt as system message.
    first_call = model.seen[0]
    assert "# Role: Product Manager" in first_call[0].content
    assert "Users can sign up from the iOS app" in first_call[1].content
    assert any(e.startswith("→ handoff_write") for e in events)
    audit = [yaml.safe_load(line) for line in open(result.audit_path)]
    assert [e["event"] for e in audit if e["event"] in ("agent_start", "handoff_written", "agent_end")] == \
        ["agent_start", "handoff_written", "agent_end"]


def test_runner_reminds_once_when_nothing_was_written(ws, store, example):
    model = ScriptedModel(script=[AIMessage("I think the spec is clear."), write_spec(example), AIMessage("Done.")])
    result = run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store, expect=["spec"])
    assert result.ok and result.outputs == ["spec-001"]
    reminder = model.seen[1][-1]
    assert isinstance(reminder, HumanMessage) and "haven't written" in reminder.content


def test_runner_reports_no_output(ws, store):
    model = ScriptedModel(script=[AIMessage("No."), AIMessage("Still no.")])
    result = run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store, expect=["spec"])
    assert result.stop_reason == "no_output" and not result.ok


def test_model_call_limit_stops_the_loop(ws_copy, tmp_path):
    root, edit = ws_copy
    edit("roles/pm.yaml", "  max_iterations: 25", "  max_iterations: 3")
    ws = load_workspace(root)
    store = HandoffStore(ws, "ridexpress", tmp_path / "h2")
    store.put(yaml.safe_load((root / "examples/ridexpress-uc1/idea-001.yaml").read_text()))
    model = ScriptedModel(script=[tool_call("rag_search", query=f"q{i}") for i in range(10)])
    result = run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store, expect=["spec"])
    assert result.stop_reason == "model_call_limit" and result.model_calls == 3


def test_a_limit_reached_after_writing_still_counts_as_finished(ws_copy, tmp_path):
    root, edit = ws_copy
    edit("roles/pm.yaml", "  max_iterations: 25", "  max_iterations: 3")
    ws = load_workspace(root)
    store = HandoffStore(ws, "ridexpress", tmp_path / "h2")
    store.put(yaml.safe_load((root / "examples/ridexpress-uc1/idea-001.yaml").read_text()))
    example = {"spec-001": yaml.safe_load((root / "examples/ridexpress-uc1/spec-001.yaml").read_text())}
    model = ScriptedModel(script=[write_spec(example)] + [tool_call("handoff_list") for _ in range(5)])
    result = run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store, expect=["spec"])
    assert result.model_calls == 3 and result.outputs == ["spec-001"]
    assert result.stop_reason == "finished" and result.ok


def test_requirements_fail_before_any_model_call(ws, store, example, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    from agentfw.tools import RequirementsError
    model = ScriptedModel(script=[])
    with pytest.raises(RequirementsError):
        run_role(ws, "ridexpress", "developer", task=example["task-001"], model=model, handoffs=store)
    assert model.seen == []


def test_cli_handoffs_import_list_show(ws, tmp_path, capsys):
    from agentfw.cli import main
    d = tmp_path / "hd"
    idea = str(ws.project_handoffs("ridexpress")["idea-001"].path)
    assert main(["handoffs", "ridexpress", "import", idea, "--handoffs-dir", str(d)]) == 0
    assert main(["handoffs", "ridexpress", "list", "--handoffs-dir", str(d)]) == 0
    assert "idea-001" in capsys.readouterr().out
    assert main(["handoffs", "ridexpress", "show", "idea-001", "--handoffs-dir", str(d)]) == 0
    assert "Users can sign up from the iOS app" in capsys.readouterr().out


def test_only_transient_model_errors_are_retried():
    from agentfw.models import is_transient

    class Status(Exception):
        def __init__(self, code):
            self.status_code = code

    class APITimeoutError(Exception):
        pass

    assert is_transient(Status(429)) and is_transient(Status(503)) and is_transient(APITimeoutError())
    assert not is_transient(Status(404)) and not is_transient(Status(401)) and not is_transient(ValueError())


def test_older_tool_results_are_cleared_from_what_the_model_sees(ws, store, example, monkeypatch):
    monkeypatch.setattr("agentfw.agent.CONTEXT_TRIGGER_TOKENS", 1)
    monkeypatch.setattr("agentfw.agent.KEEP_TOOL_RESULTS", 1)
    model = ScriptedModel(script=[
        tool_call("handoff_list"), tool_call("handoff_list"), tool_call("handoff_list"),
        AIMessage("Done."),
    ])
    run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store)
    results = [m.content for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert results[:2] == [CLEARED_NOTE, CLEARED_NOTE]
    assert "idea-001" in results[2]                                               # newest kept


def test_agent_is_told_to_wrap_up_before_the_limit(ws_copy, tmp_path):
    root, edit = ws_copy
    edit("roles/pm.yaml", "  max_iterations: 25", "  max_iterations: 6")      # warned once 3 calls are left
    ws = load_workspace(root)
    store = HandoffStore(ws, "ridexpress", tmp_path / "h3")
    store.put(yaml.safe_load((root / "examples/ridexpress-uc1/idea-001.yaml").read_text()))
    example = {"spec-001": yaml.safe_load((root / "examples/ridexpress-uc1/spec-001.yaml").read_text())}

    def write_when_told(messages):
        assert isinstance(messages[-1], HumanMessage) and messages[-1].content == WRAP_UP
        return write_spec(example)

    model = ScriptedModel(script=[tool_call("rag_search", query=f"q{i}") for i in range(3)] +
                          [write_when_told, AIMessage("Wrote spec-001.")])
    result = run_role(ws, "ridexpress", "pm", inputs=["idea-001"], model=model, handoffs=store, expect=["spec"])
    assert result.ok and result.model_calls == 5
    assert sum(isinstance(m, HumanMessage) and m.content == WRAP_UP for m in model.seen[-1]) == 1   # only once
    assert "rag_search" in model.tool_sets[0]
    assert model.tool_sets[3] and all(t.startswith("handoff_") for t in model.tool_sets[3])        # after the warning

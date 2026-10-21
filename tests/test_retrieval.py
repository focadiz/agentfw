"""Chunking, the knowledge index, rag.search and project memory.

Unit tests use the offline hashing embedder. The integration test at the end
uses the real model on the real RideXpress docs: pytest -m integration
"""
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentfw.permissions import Permissions
from agentfw.resolver import Knowledge, resolve
from agentfw.retrieval import KnowledgeIndexError, ProjectIndex, chunk_file, sources_from
from agentfw.tools import build_toolset
from agentfw.tools import rag

SIGNUP = """# Sign Up – Edge Conditions

Intro text.

## Edge Conditions

### EC-1: Verification Code Expiration

The verification code has a TTL of 10 minutes.

### EC-3: Email Already Registered

If the email already exists, the system returns: "This email is already registered."
A "Go to Login" button is provided.
"""

RAML = """#%RAML 1.0
title: Experience API
/user:
  post:
    description: User Sign Up
/rides:
  get:
    description: List rides
  /{id}:
    get:
      description: Get one ride
"""


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTFW_EMBEDDINGS", "hashing")
    rag.reset_cache()
    yield
    rag.reset_cache()


@pytest.fixture
def corpus(tmp_path):
    docs = tmp_path / "proj" / "docs"
    (docs / "use-cases").mkdir(parents=True)
    (docs / "use-cases" / "sign-up-edge-conditions.md").write_text(SIGNUP)
    (docs / "use-cases" / "payments.md").write_text("# Payments\n\nSquare charges the rider's card after the ride.\n")
    (docs / "use-cases" / "rides.md").write_text("# Rides\n\nA driver accepts a ride request and drives to the pickup.\n")
    (docs / "confidential").mkdir()
    (docs / "confidential" / "salaries.md").write_text("# Salaries\n\nEmail already registered salary table.\n")
    api = tmp_path / "proj" / "api"
    api.mkdir()
    (api / "experience.raml").write_text(RAML)
    return tmp_path / "proj"


def knowledge(root):
    return (
        Knowledge("role", "project-docs", "repo_glob", f"{root}/docs/**/*.md"),
        Knowledge("ios", "api-contract", "repo_glob", f"{root}/api/*.raml"),
        Knowledge("ios", "apple-docs", "vector_index", "apple-swiftui-hig"),
    )


# ---- Chunking ------------------------------------------------------------------

def test_markdown_chunks_carry_heading_trail_and_line():
    chunks = chunk_file("/x/sign-up.md", SIGNUP)
    ec3 = next(c for c in chunks if "EC-3" in c.heading)
    assert ec3.heading == "Sign Up – Edge Conditions > Edge Conditions > EC-3: Email Already Registered"
    assert ec3.start_line == SIGNUP.splitlines().index("### EC-3: Email Already Registered") + 1
    assert ec3.embedded_text.startswith("sign-up.md — Sign Up")
    assert "  \n" not in ec3.text


def test_raml_splits_at_resources():
    long_raml = RAML + "".join(f"/r{i}:\n  get:\n    description: {'x' * 200}\n" for i in range(12))
    chunks = chunk_file("/x/api.raml", long_raml)
    assert len(chunks) > 1
    assert any(c.heading.startswith("/r") for c in chunks)
    for c in chunks:
        assert long_raml.splitlines()[c.start_line - 1].strip() == c.text.splitlines()[0].strip()


# ---- Index -------------------------------------------------------------------

def test_index_sync_is_incremental(corpus):
    index = ProjectIndex("demo")
    srcs = sources_from(knowledge(corpus))
    first = index.sync(srcs)
    assert (first.added, first.updated, first.removed) == (5, 0, 0)
    assert "apple-swiftui-hig" in first.skipped

    again = ProjectIndex("demo").sync(srcs)                   # fresh object, persisted state
    assert (again.added, again.unchanged, again.chunks) == (0, 5, 0)

    (corpus / "docs" / "use-cases" / "rides.md").write_text("# Rides\n\nChanged.\n")
    (corpus / "docs" / "use-cases" / "payments.md").unlink()
    third = ProjectIndex("demo").sync(srcs)
    assert (third.added, third.updated, third.removed, third.unchanged) == (0, 1, 1, 3)
    assert "payments.md" not in json.dumps(ProjectIndex("demo").manifest)


def test_search_finds_the_right_doc_and_respects_permissions(corpus):
    index = ProjectIndex("demo")
    srcs = sources_from(knowledge(corpus))
    index.sync(srcs)

    hits = index.search("email already registered sign up", srcs, k=3)
    assert hits[0].path.endswith("sign-up-edge-conditions.md")
    assert hits[0].source_ids == ("project-docs",)

    readable = lambda p: "/confidential/" not in p                 # noqa: E731
    paths = [h.path for h in index.search("email already registered salary", srcs, k=5, readable=readable)]
    assert not any("salaries" in p for p in paths)


def test_search_is_limited_to_the_given_sources(corpus):
    index = ProjectIndex("demo")
    all_srcs = sources_from(knowledge(corpus))
    index.sync(all_srcs)
    api_only = [s for s in all_srcs if s.ids == ("api-contract",)]
    hits = index.search("user sign up", api_only, k=3)
    assert hits and all(h.path.endswith(".raml") for h in hits)


def test_changing_the_embedding_model_requires_a_rebuild(corpus, monkeypatch):
    srcs = sources_from(knowledge(corpus))
    ProjectIndex("demo").sync(srcs)
    other = ProjectIndex("demo", embeddings="fastembed:some/other-model")
    with pytest.raises(KnowledgeIndexError, match="--rebuild"):
        other.search("anything", srcs)


# ---- rag.search through the toolset --------------------------------------------

@pytest.fixture
def pm(ws, corpus):
    base = resolve(ws, "ridexpress", "pm")
    perms = Permissions.from_role({"fs_read": [f"{corpus}/**"], "fs_deny": ["**/confidential/**"]})
    r = replace(base, project="demo", project_root=str(corpus), knowledge=knowledge(corpus), permissions=perms)
    ProjectIndex("demo").sync(sources_from(r.knowledge))
    return build_toolset(r, check_requirements=False)


def test_rag_search_tool(pm):
    result = pm.get("rag.search").invoke({"query": "what if the email is already registered", "k": 2})
    first = result.splitlines()[2]
    assert "sign-up-edge-conditions.md:" in first and "EC-3" in first
    assert "Not searched" in result and "apple-docs" in result          # vector_index source reported
    assert "salaries" not in result                                     # denied by fs_deny


def test_rag_search_rejects_unknown_sources(pm):
    result = pm.get("rag.search").invoke({"query": "x", "sources": ["nope"]})
    assert result.startswith("Error: unknown source(s) ['nope']")


def test_rag_search_before_indexing(ws, tmp_path):
    r = replace(resolve(ws, "ridexpress", "pm"), project="empty",
                knowledge=(Knowledge("role", "docs", "repo_glob", f"{tmp_path}/none/*.md"),))
    result = build_toolset(r, check_requirements=False).get("rag.search").invoke({"query": "x"})
    assert result.startswith("No results") and "fw index empty" in result


# ---- Memory ------------------------------------------------------------------

def test_memory_write_and_read(ws):
    task = copy.deepcopy(ws.project_handoffs("ridexpress")["task-001"].data)
    dev = build_toolset(resolve(ws, "ridexpress", "developer", task), check_requirements=False)
    write, read = dev.get("memory.write"), dev.get("memory.read")
    assert read.invoke({}) == "No notes yet."
    write.invoke({"note": "mule-maven-plugin 4.7.0 fails on Maven 3.10; use 3.9.x", "tags": ["build"]})
    write.invoke({"note": "Okta system API expects the transactionId header", "tags": ["okta"]})
    recent = read.invoke({})
    assert recent.splitlines()[1].endswith("transactionId header  [okta]")       # newest first
    ranked = read.invoke({"query": "maven build failure"})
    assert "Maven 3.10" in ranked.splitlines()[1]


def test_memory_scope_project_is_shared_across_tasks(ws):
    tasks = ws.project_handoffs("ridexpress")
    a = build_toolset(resolve(ws, "ridexpress", "developer", tasks["task-001"].data), check_requirements=False)
    b = build_toolset(resolve(ws, "ridexpress", "developer", tasks["task-002"].data), check_requirements=False)
    a.get("memory.write").invoke({"note": "Prefer small PRs"})
    assert "Prefer small PRs" in b.get("memory.read").invoke({})


def test_memory_refuses_credentials(ws):
    pm = build_toolset(resolve(ws, "ridexpress", "pm"), check_requirements=False)
    for secret in ("password = hunter2hunter2", "token: ghp_" + "a" * 36, "AKIAABCDEFGHIJKLMNOP"):
        result = pm.get("memory.write").invoke({"note": f"remember {secret}"})
        assert result.startswith("Error:") and "credential" in result
    assert pm.get("memory.read").invoke({}) == "No notes yet."


# ---- Integration: real model, real RideXpress docs --------------------------------

@pytest.mark.integration
def test_real_index_answers_the_signup_question(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFW_EMBEDDINGS", "fastembed:BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("AGENTFW_HOME", str(Path.home() / ".agentfw"))   # reuse the downloaded model
    monkeypatch.setenv("AGENTFW_WORK", str(tmp_path / "work"))
    rag.reset_cache()
    project = ws.projects["ridexpress"].data
    if not Path(project["docs_root"]).is_dir():
        pytest.skip("RideXpress checkout not found")
    index = ProjectIndex("ridexpress", root=tmp_path / "index")
    from agentfw.retrieval import project_sources
    srcs = project_sources(ws, "ridexpress")
    index.sync(srcs)
    pm_sources = sources_from(resolve(ws, "ridexpress", "pm").knowledge)
    hits = index.search("What should happen when someone signs up with an email that is already registered?", pm_sources, k=3)
    assert hits[0].path.endswith("sign-up-edge-conditions.md") and "EC-3" in hits[0].heading

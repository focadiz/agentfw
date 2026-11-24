# Agent Framework (draft v0.1)

A team of role agents (PM, Architect, Developer, QA, DevOps) that builds software on any platform through plug-in **skill packs**. Roles define *what* an agent does; packs define *how* on a given platform (MuleSoft, iOS, ...).

This folder holds the configuration layer (schemas, role and pack configs, the tool registry, prompts, a worked example) and the `agentfw` Python package. Done so far:
- **M1:** config loading, the resolver that merges role + pack + project, validation, the `fw` CLI.
- **M2:** tools. The tool factory turns a resolved role into LangChain tools, every call goes through the permission guard, pack commands run without a shell, and everything is written to an audit log.
- **M3:** retrieval and memory. A local knowledge index over each project's docs and API specs (Chroma + local embeddings), `rag.search` with MMR, and per-role project memory.
- **M4:** agents. Any role runs as a LangChain `create_agent` loop (`fw run`), writing validated handoff documents through the handoff store.
- **M5:** the pipeline. A LangGraph graph runs an idea through PM and Architect, pausing at each approval gate until a human runs `fw approve`, `fw revise` or `fw reject`.

Developer ⇄ QA on a real MuleSoft task comes next.

## Setup

Requires Python 3.11+ (LangChain 1.x needs 3.10+; macOS's built-in 3.9 is too old).

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/fw validate                                   # check every config and handoff
.venv/bin/fw resolve ridexpress developer --task task-002   # see what an agent would get
.venv/bin/fw tools ridexpress developer --task task-001     # its tools, and any unmet host requirements
.venv/bin/fw call ridexpress developer --task task-001 fs.read --arg path=user-sign-up-process-api/pom.xml
.venv/bin/fw checks ridexpress developer --task task-001 build   # run a verification check
.venv/bin/fw index ridexpress                                # build/update the knowledge index
.venv/bin/fw search ridexpress pm "what if the email is already registered?"
echo 'OPENROUTER_API_KEY=sk-or-...' > .env                   # git-ignored; fw loads it
.venv/bin/fw handoffs ridexpress import examples/ridexpress-uc1/idea-001.yaml --handoffs-dir .work/handoffs/ridexpress
.venv/bin/fw run ridexpress pm --input idea-001 --expect spec --handoffs-dir .work/handoffs/ridexpress
.venv/bin/fw pipeline ridexpress idea-001 --handoffs-dir .work/handoffs/ridexpress   # runs until the first gate
.venv/bin/fw approve ridexpress spec-001 --handoffs-dir .work/handoffs/ridexpress    # ...and continues
.venv/bin/pytest                      # unit tests (offline)
.venv/bin/pytest -m integration       # real embedding model on the real RideXpress docs
```

```
schema/                 JSON Schemas for every file type
  role.schema.json
  skill-pack.schema.json
  project.schema.json
  handoff.schema.json   # all handoff document types
  tool-registry.schema.json
roles/                  pm, architect, developer, qa, devops (+ prompts/)
skill-packs/            mulesoft, ios, mulesoft-cloudhub (+ prompts/)
tools/registry.yaml     framework tools (files, git, GitHub via MCP, retrieval, memory, handoffs, human)
projects/ridexpress.yaml
examples/ridexpress-uc1/  UC-1 User Sign Up through the pipeline (illustrative)
src/agentfw/
  config.py             # load schemas and YAML into a Workspace
  variables.py          # ${namespace.name} expansion
  permissions.py        # role ceiling ∩ pack request
  resolver.py           # project + role + pack(s) [+ task] → Resolved
  validation.py         # schema + cross-reference checks
  cli.py                # fw validate | resolve | tools | requirements | call | checks | index | search | handoffs | run
  agent.py              # run a role: prompt + tools + create_agent + limits → handoff documents
  models.py             # role model block → LangChain chat model (OpenRouter, OpenAI, Anthropic, Ollama)
  handoffs.py           # handoff store: ids, envelope, status, validation
  testing.py            # ScriptedModel, for running the agent loop offline
  tools/
    factory.py          # Resolved → LangChain StructuredTools, each wrapped with validation + audit
    guard.py            # path / command / host checks, symlink-aware
    runner.py           # pack commands and checks: no shell, one argument per ${args.*}, timeouts
    requirements.py     # OS, binary versions and env vars a pack needs
    fs.py               # fs.read / fs.list / fs.search / fs.write
    audit.py            # JSONL audit log + full command output per run
    rag.py              # rag.search
    memory.py           # memory.read / memory.write
    handoff.py          # handoff.read / handoff.list / handoff.write
    human.py            # human.ask (deferred, console or scripted)
  retrieval/
    chunking.py         # Markdown by heading, RAML by resource, keeps file + line
    embeddings.py       # local embeddings (fastembed, bge-small-en-v1.5)
    index.py            # per-project Chroma index, incremental sync, scoped search
tests/
```

## Pipeline

```
human: idea
  └─ PM ──────────► spec ──[gate: human approves]──► task_list
       └─ Architect ─► adr(s) + design ──[gate: human approves]──► task (one per platform/PR)
            └─ Developer[pack] ─► pull_request
                 └─ QA[pack] ─► test_report
                      ├─ fail → back to Developer (max_retries, then human)
                      └─ pass → human merges
                           └─ DevOps[deploy pack] ─► deploy_request ──[gate]──► deploy_report
```

Each arrow is a **handoff document** (`schema/handoff.schema.json`), stored as `<handoffs_dir>/<id>.yaml`. Documents list their `parents`, so any PR traces back to the requirement and decision behind it.

Gates are the human-intervention points. Merging is never delegated: no role has `can_merge`. For RideXpress MuleSoft, merging to `main` triggers the deploy workflow, so the merge is the deploy approval.

## Roles

| Role | Consumes | Produces | Packs | Gate |
|---|---|---|---|---|
| PM | idea, test_report | spec, task_list | none | spec |
| Architect | spec, task_list, adr, design, test_report | design, adr, task | knowledge from all project platforms | design |
| Developer | task, design, adr, test_report | pull_request | one, by `task.platform` | (the PR) |
| QA | task, pull_request, design | test_report | one, by `task.platform` (knowledge, tools, checks) | (the merge) |
| DevOps | test_report, pull_request, deploy_report | deploy_request, deploy_report | the platform's `deploy_pack` | deploy_request |

## How a task is resolved

1. The task's `platform` (e.g. `ios`) is looked up in the project's `platforms` to find its pack, repo and variable values.
2. Pack `requirements` are checked (OS, binaries, version ranges, env vars) for packs whose commands the role runs. Pack commands refuse to run until they're met; the pipeline will also check up front, before any LLM call.
3. Role and pack are merged:

| Field | Rule |
|---|---|
| `instructions` | Role prompt, then pack prompt |
| `tools`, `knowledge` | Role's + pack's |
| `verification` | Pack only |
| `permissions` | **Intersection**: pack request ∩ role ceiling. `fs_deny` is the **union**. `git` from the role only |
| `model`, `limits`, `gates`, `memory` | Role only |

A role's `skill_packs.use` limits which parts load. The Architect takes only `knowledge`, so it never gets build tools.

A role's `skill_packs.run_in` sets where pack commands run: `repo` (the platform's repo, for the Developer) or `checkout` (a clean checkout of the PR, for QA). Pack commands are written relative to that directory, and pack write globs use `${platform.worktree}`, so the same pack works for both roles.

`fw resolve <project> <role> [--task <id>]` prints the merged result: packs, tools with fully expanded commands, checks, knowledge sources, effective permissions and where commands run.

## Variables

Used in commands, globs, knowledge locations and prompts. Values can reference other variables (e.g. `health_url` uses `${pack.app}`); they're expanded at task time. A reference to an undefined variable is an error, so a missing setting can never expand to something broad like `/**`. Text that only looks like a variable, such as Mule's `${secure::key}`, is left alone.

| Variable | Source |
|---|---|
| `${project.root}`, `${project.docs_root}`, `${project.handoffs_dir}`, `${project.name}`, `${project.platforms}` | Project config |
| `${pack.<name>}` | Pack variables. `scope: project` values come from the project's platform `variables`; `scope: task` values come from the task's `pack_variables` |
| `${platform.repo_root}`, `${platform.repo_remote}`, `${platform.worktree}` | The pack's platform. `worktree` is the repo for `run_in: repo` roles and the clean checkout for `run_in: checkout` roles |
| `${task.id}`, `${task.platform}`, `${task.repo_root}`, `${task.workdir}`, `${task.checkout}` | The current task. `workdir` is a scratch dir outside the repos (`~/.agentfw/work/<project>/<task>`, override with `AGENTFW_WORK`); QA's clean checkout is `checkout` inside it |
| `${role.id}`, `${role.branch_prefix}` | The role |
| `${args.<name>}` | Tool-call arguments from the agent, validated against each arg's `pattern` |

## Tools

`tools/registry.yaml` defines the framework tools roles can list. GitHub tools are backed by the GitHub MCP server; the rest are built-in. Every tool declares its `effect` (read / local / remote) and which permissions guard it.

There is **no generic shell tool**. Agents can only run commands a pack defines, with validated arguments.

How a tool call is handled:
1. **Arguments** are validated against the tool's JSON Schema. Bad arguments come back to the agent as `Error: ...` so it can retry.
2. **The guard** checks every path (as written *and* after resolving symlinks, so a link can't point outside the allowed tree), every command name, and every host. Blocked calls come back as `Denied: <reason>`, e.g. `matches deny rule '**/src/main/resources/*secure*'` or `outside the allowed write paths [...]`.
3. **Pack commands** are split into arguments first, then each `${args.x}` fills exactly one argument, so a value can't add flags or chain commands. They run with a minimal environment (no inherited secrets beyond what the pack lists in `requirements.env`), in the role's worktree, in their own process group so a timeout kills child processes too. The agent sees the start and end of the output; the full output goes to the run's log.
4. **Host requirements** (OS, binary versions, env vars) are checked before the first pack command. If they're not met, pack commands and checks return an error listing what's missing; other tools keep working.
5. **Every call** is written to `~/.agentfw/logs/<project>/<run_id>/audit.jsonl` (override the root with `AGENTFW_HOME`): tool, arguments (file contents logged by size only), outcome (ok / denied / error), reason and duration. Command output files sit next to it.

Tool names are sent to the model with dots replaced (`fs.read` → `fs_read`), since LLM APIs only accept `[a-zA-Z0-9_-]`. Checks are exposed as one `run_check` tool whose `check_id` is limited to the task's checks.

**Known limit:** the network allowlist applies to framework tools, not to what a pack command does itself (Maven still downloads dependencies). Sandboxing subprocesses (e.g. macOS `sandbox-exec`) is future work.

## Knowledge and memory

**Index.** `fw index <project>` collects every `repo_glob` knowledge source that any enabled role can search, chunks the files and embeds them into one Chroma collection at `~/.agentfw/index/<project>/`. Re-running only re-embeds files whose content changed (and drops deleted ones), so it's cheap to run before every pipeline run. RideXpress (46 docs, 16 RAML specs, ~540 chunks) indexes in about 17 seconds.

- **Chunking** keeps each chunk's place: Markdown is split by heading and records the heading trail (`Sign Up – Edge Conditions > … > EC-3: Email Already Registered`), RAML is split at resources (`/user`, `/rides/{id}`), and every chunk knows its file and starting line.
- **Embeddings** run locally: `BAAI/bge-small-en-v1.5` through fastembed (ONNX, no PyTorch, ~130 MB downloaded once to `~/.agentfw/models`). Free, and project docs never leave the machine. `AGENTFW_EMBEDDINGS` switches models; the index records which model built it and asks for `--rebuild` if that changes.

**`rag.search`** returns the top chunks with `path:line`, heading and source id, so agents can cite them and open the file with `fs.read`. It uses MMR (maximal marginal relevance) by default, so results cover different documents instead of five near-identical chunks. Search is scoped twice: to the knowledge sources the calling role lists, then to files it may read. The shared index can't widen what a role sees.

**Memory.** `memory.write` appends a short note to `~/.agentfw/memory/<project>/<role>.jsonl` (role `memory.scope: project`) or a per-task file (`scope: task`). `memory.read` returns the newest notes, or ranks them against a query using the same embeddings. Notes that look like credentials are refused.

**Not indexed yet:** `vector_index` sources (pre-built external docs such as Mule 4/DataWeave or SwiftUI) and `url` sources. `rag.search` tells the agent which sources it couldn't search.

## Running a role

`fw run <project> <role> --input <handoff-id> [--expect <type>]` runs one role as an agent and streams its tool calls.

- **Prompt:** the role and pack instructions, then run context (date, project, where handoffs live), then the exact body schema of every document type the role produces. Input documents go inline in the first message, so the agent doesn't spend calls fetching them.
- **Loop:** LangChain `create_agent` with the role's tools. `LimitsMiddleware` enforces the role's `limits`: model calls (`max_iterations`), tokens (`max_tokens_per_task`) and time (`timeout_minutes`). If the role has `model.fallback`, `ModelFallbackMiddleware` switches models when a call fails.
- **Output:** agents write documents with `handoff_write`. The **handoff store** assigns the id, records the author and run id, sets the status (`pending_approval` if the role has a gate on that type), and validates the whole document (schema, parents exist, unique requirement ids, stories that reference real requirements, design coverage...). A rejected document comes back to the agent as a list of problems to fix.
- **Expected outputs:** with `--expect spec`, an agent that stops without writing a spec gets one reminder, then the run is reported as `no_output`.
- **Questions:** `human_ask` is deferred by default: the question is logged and the agent is told to record it as an open question. `--human console` asks in the terminal instead. In the pipeline, deferred questions are shown with the run when it stops at a gate.
- **Host requirements** are checked before the first model call for roles that run pack commands, so a broken toolchain costs no tokens.
- **Result:** stop reason (`finished`, `no_output`, `model_call_limit`, `token_budget`, `timeout`), documents written, model and tool call counts, tokens, and the audit log path.

`--handoffs-dir` points a run at another folder, e.g. `.work/handoffs/<project>` for trial runs, so nothing is written to the project's real `handoffs_dir` until you're ready.

**Models:** the PM uses `nvidia/nemotron-3-ultra-550b-a55b` (paid, about $0.25 per spec) with the free Nemotron and `google/gemma-4-31b-it:free` as fallbacks; the free models alone hit upstream rate limits too often. The other roles still have `<model-id>` placeholders until each is tried. `fw run --model <id>` overrides for one run.

## The pipeline

`fw pipeline <project> <idea>` runs an approved idea through the roles as a LangGraph graph (`fw pipeline <project> --graph` prints it as Mermaid):

```
idea → pm: spec → ⏸ approve spec → pm: task_list → architect: design (+ ADRs) → ⏸ approve design → architect: tasks → ready for development
```

- **The handoff store is the source of truth.** Each step first looks for the document it would write (its type, with its input in `parents`). If it exists, the role doesn't run again. So a pipeline can be started over, or pick up documents written by hand or by `fw run`, without paying for the same work twice.
- **Gates** come from the roles' `gates` (`before_handoff`). A document waiting for approval pauses the graph with LangGraph's `interrupt()`. The command prints the document's path and the three ways on:
  - `fw approve <project> <id> [-m comment]` → status `approved`, and the pipeline continues.
  - `fw revise <project> <id> -m "what to change"` → status `draft`; the author role runs again with the comment and revises the same document (same id), which comes back to the gate.
  - `fw reject <project> <id> -m "why"` → status `rejected`; the pipeline ends.
  The decision is recorded on the document (`approval: {by, at, decision, comment}`). `--by` defaults to the project's only approver, and must be one of `approvers`. `--no-resume` records the decision without continuing.
- **Pausing survives between commands.** Checkpoints are kept in `~/.agentfw/pipelines/<project>.sqlite`, one thread per idea and handoffs folder. If a step fails (no output, a missing model id), fix the cause and run `fw pipeline` again: it continues from the last checkpoint.
- `fw pipeline <project> <idea> --status` shows the runs so far (stop reason, outputs, calls, tokens, questions) and what it's waiting on, without running anything.

## Validate

```bash
.venv/bin/fw validate
```

It checks every YAML file against its schema, then the cross-references between files:
- role tools exist in the registry; prompt files exist
- packs stay within each role's permission ceiling (commands, network), and only run executables they allow
- every `${pack.*}` / `${args.*}` reference is declared
- the pipeline is wired: everything an enabled role consumes is produced by some role
- project platforms point at real packs and repos, and supply every required variable
- tasks name real checks and supply task-scoped variables
- designs cover every `must`/`should` requirement in their spec
- test report verdicts are consistent with their check and acceptance results
- every enabled role resolves (against each example task, for roles that need one)

## Not built yet

- External documentation indexes (`vector_index` sources) and `url` sources
- Remaining built-in tools: `git.*`, `handoff.*`, `human.ask`, `web.fetch` (M4–M6), and the GitHub MCP connection (M6). `fw tools` lists what's still unavailable for a role
- M6: Developer ⇄ QA on a real MuleSoft task (needs `git.*` and the GitHub MCP tools)
- Salesforce pack (the Developer role already allows it; the validator warns until it exists)
- An iOS deploy pack. TestFlight/App Store stays manual for now

## Open TODOs in the configs

- `projects/ridexpress.yaml`: confirm the CloudHub `health_url` pattern; create the Xcode project in `mobile-app` by hand before the first iOS task
- Roles: choose real model ids for architect, developer, qa and devops (pm is set)
- Local MuleSoft builds need **Maven 3.9.x and Java 17** (what CI uses). This machine has Maven 3.10.0, which breaks `mule-maven-plugin` 4.7.0, and Java 27; `fw requirements ridexpress developer --task task-001` shows the current state
- `tools/registry.yaml`: confirm the GitHub MCP tool names against the server version you install

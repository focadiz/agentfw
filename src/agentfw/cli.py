"""fw: command-line entry point."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Workspace, find_root, load_workspace
from .resolver import Resolved, ResolutionError, resolve
from .validation import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fw", description="Agent framework CLI")
    parser.add_argument("--root", type=Path, help="framework root (default: auto-detect)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="validate configs and handoff documents")

    ix = sub.add_parser("index", help="build or update a project's knowledge index")
    ix.add_argument("project")
    ix.add_argument("--rebuild", action="store_true", help="drop the index and embed everything again")
    ix.add_argument("--status", action="store_true", help="show what's indexed without changing anything")

    ho = sub.add_parser("handoffs", help="list, show or import handoff documents")
    ho.add_argument("project")
    ho.add_argument("action", choices=["list", "show", "import"])
    ho.add_argument("target", nargs="?", help="document id (show) or YAML file (import)")
    ho.add_argument("--type")
    ho.add_argument("--status")
    ho.add_argument("--handoffs-dir", type=Path, help="use this folder instead of the project's handoffs_dir")

    rn = sub.add_parser("run", help="run one role as an agent")
    rn.add_argument("project")
    rn.add_argument("role")
    rn.add_argument("--input", action="append", default=[], help="handoff id given to the agent (repeatable)")
    rn.add_argument("--task", help="task handoff id, for roles that need one")
    rn.add_argument("--expect", action="append", default=[], help="document type the agent must write (repeatable)")
    rn.add_argument("--model", help="model id, overriding the role and project config")
    rn.add_argument("--handoffs-dir", type=Path, help="use this folder instead of the project's handoffs_dir")
    rn.add_argument("--human", choices=["defer", "console"], default="defer",
                    help="defer: questions become open questions (default); console: ask in this terminal")
    rn.add_argument("--quiet", action="store_true", help="don't stream the agent's steps")

    pl = sub.add_parser("pipeline", help="run an idea through the roles, pausing at approval gates")
    pl.add_argument("project")
    pl.add_argument("idea", nargs="?", help="idea handoff id (start, or continue after a decision or an error)")
    pl.add_argument("--status", action="store_true", help="show where the pipeline is without running anything")
    pl.add_argument("--graph", action="store_true", help="print the pipeline graph as Mermaid")
    pl.add_argument("--model", help="model id for every role, overriding their configs")
    pl.add_argument("--handoffs-dir", type=Path, help="use this folder instead of the project's handoffs_dir")
    pl.add_argument("--quiet", action="store_true", help="don't stream the agents' steps")

    for name, help in (("approve", "approve a document waiting at a gate, then continue its pipeline"),
                       ("revise", "send a document back to its author with the changes you want"),
                       ("reject", "reject a document; its pipeline ends")):
        d = sub.add_parser(name, help=help)
        d.add_argument("project")
        d.add_argument("handoff", help="id of the document waiting for approval")
        d.add_argument("-m", "--comment", required=name != "approve", help="what to change, or why")
        d.add_argument("--by", help="who decides (default: the project's only approver)")
        d.add_argument("--no-resume", action="store_true", help="record the decision without continuing the pipeline")
        d.add_argument("--model", help="model id for every role, overriding their configs")
        d.add_argument("--handoffs-dir", type=Path, help="use this folder instead of the project's handoffs_dir")
        d.add_argument("--quiet", action="store_true", help="don't stream the agents' steps")

    def role_command(name: str, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("project")
        p.add_argument("role")
        p.add_argument("--task", help="task handoff id, for roles that pick a pack by platform")
        p.add_argument("--allow-disabled", action="store_true", help="resolve even if the role is disabled")
        return p

    r = role_command("resolve", "show a role's effective configuration")
    r.add_argument("--json", action="store_true", help="print as JSON")
    r.add_argument("--show-prompt", action="store_true", help="include the full combined prompt")

    role_command("tools", "list the tools an agent in this role gets")
    role_command("requirements", "check the host has what the role's packs need")

    c = role_command("call", "call one tool as the agent would, through the guard")
    c.add_argument("tool", help="tool id or name, e.g. fs.read or mule.build")
    c.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE", help="tool argument (repeatable)")
    c.add_argument("--skip-requirements", action="store_true", help="run even if host requirements aren't met")

    s = role_command("search", "search the role's knowledge sources, as rag.search would")
    s.add_argument("query")
    s.add_argument("--source", action="append", help="limit to a knowledge source id (repeatable)")
    s.add_argument("-k", type=int, default=6, help="number of results (default 6)")
    s.add_argument("--no-mmr", action="store_true", help="plain similarity ranking instead of MMR")

    k = role_command("checks", "run the task's verification checks")
    k.add_argument("check_ids", nargs="*", help="checks to run (default: all)")
    k.add_argument("--skip-requirements", action="store_true", help="run even if host requirements aren't met")
    k.add_argument("--json", action="store_true", help="print results in handoff check_result format")

    args = parser.parse_args(argv)
    try:
        root = args.root or find_root()
    except FileNotFoundError as e:
        return _fail(str(e), 2)
    load_dotenv(Path(root) / ".env")
    ws = load_workspace(root)

    report = validate(ws)
    if args.command == "validate":
        print(report.format())
        return 1 if report.errors else 0
    if report.errors:
        print(report.format(), file=sys.stderr)
        return _fail("fix validation errors first")
    if args.command == "index":
        return _cmd_index(ws, args)
    if args.command == "handoffs":
        return _cmd_handoffs(ws, args)
    if args.command == "run":
        return _cmd_run(ws, args)
    if args.command == "pipeline":
        return _cmd_pipeline(ws, args)
    if args.command in ("approve", "revise", "reject"):
        return _cmd_decide(ws, args)

    try:
        resolved = _resolve(ws, args)
    except (ResolutionError, LookupError) as e:
        return _fail(str(e), 2 if isinstance(e, LookupError) else 1)

    handlers = {"resolve": _cmd_resolve, "tools": _cmd_tools, "requirements": _cmd_requirements,
                "call": _cmd_call, "checks": _cmd_checks, "search": _cmd_search}
    return handlers[args.command](resolved, args)


# ---- Commands ----------------------------------------------------------------

def _cmd_resolve(resolved: Resolved, args) -> int:
    if args.json:
        data = resolved.to_dict()
        if not args.show_prompt:
            data["instructions"] = f"<{len(resolved.instructions)} chars; use --show-prompt>"
        print(json.dumps(data, indent=2, default=list))
    else:
        print(format_resolved(resolved, show_prompt=args.show_prompt))
    return 0


def _cmd_tools(resolved: Resolved, args) -> int:
    from .tools import build_toolset, check_requirements

    ts = build_toolset(resolved, check_requirements=False)
    print(f"{resolved.role} @ {resolved.project}" + (f"  task {resolved.task}" if resolved.task else ""))
    width = max((len(t.name) for t in ts.tools), default=10)
    for t in ts.tools:
        print(f"  {t.name:<{width}}  {ts.sources[t.name]:<16} {t.description.splitlines()[0][:70]}")
    if ts.unavailable:
        print("  not available yet:")
        for tool_id, reason in ts.unavailable.items():
            print(f"    {tool_id}: {reason}")
    problems = check_requirements(resolved)
    if problems:
        print("  host requirements not met (pack commands will refuse to run):")
        print("\n".join(f"    - {p}" for p in problems))
    print(f"  audit log: {ts.context.audit.path}")
    return 0


def _cmd_requirements(resolved: Resolved, args) -> int:
    from .tools import check_requirements

    if not resolved.requirements:
        print("no host requirements (this role runs no pack commands)")
        return 0
    problems = check_requirements(resolved)
    for pack_id, req in resolved.requirements.items():
        names = [b["name"] for b in req.get("binaries", [])] + list(req.get("env", []))
        print(f"{pack_id}: checks {', '.join(names) or 'nothing'}")
    if problems:
        print("\n".join(f"  - {p}" for p in problems))
        return 1
    print("all requirements met")
    return 0


def _cmd_call(resolved: Resolved, args) -> int:
    from .tools import build_toolset

    try:
        ts = build_toolset(resolved, check_requirements=not args.skip_requirements)
        tool = ts.get(args.tool)
    except KeyError as e:
        return _fail(e.args[0], 2)
    try:
        tool_args = _parse_args(args.arg, tool.args_schema)
    except ValueError as e:
        return _fail(str(e), 2)
    result = tool.invoke(tool_args)
    print(result)
    print(f"\naudit log: {ts.context.audit.path}", file=sys.stderr)
    return 1 if result.startswith(("Denied:", "Error:")) else 0


def _cmd_index(ws: Workspace, args) -> int:
    from .retrieval import KnowledgeIndexError, ProjectIndex, project_sources

    if args.project not in ws.projects:
        return _fail(f"unknown project {args.project!r}", 2)
    index = ProjectIndex(args.project)
    if args.status:
        rows = index.status()
        if not rows:
            print(f"{args.project}: nothing indexed yet (run: fw index {args.project})")
        for row in rows:
            print(f"  {row['files']:>4} files  {'/'.join(row['ids'])}: {row['location']}  (indexed {row['indexed_at']})")
        print(f"embeddings: {index.manifest.get('embeddings')}\nindex: {index.dir}")
        return 0
    sources = project_sources(ws, args.project)
    print(f"Indexing {len(sources)} knowledge source(s) for {args.project} with {index.embeddings_name} ...")
    try:
        report = index.sync(sources, rebuild=args.rebuild, progress=lambda p: print(f"  embedding {p}", file=sys.stderr))
    except KnowledgeIndexError as e:
        return _fail(str(e))
    print(report.format())
    print(f"index: {index.dir}")
    return 0


def _cmd_handoffs(ws: Workspace, args) -> int:
    import yaml

    from .handoffs import HandoffError, HandoffStore, dump

    if args.project not in ws.projects:
        return _fail(f"unknown project {args.project!r}", 2)
    store = HandoffStore(ws, args.project, args.handoffs_dir)
    try:
        if args.action == "list":
            docs = store.list(type=args.type, status=args.status)
            for d in docs:
                print(f"{d['id']:<18} {d['status']:<17} {d['created_by']['actor']:<10} {d['title']}")
            print(f"{len(docs)} document(s) in {store.dir}")
        elif args.action == "show":
            if not args.target:
                return _fail("show needs a document id", 2)
            print(dump(store.get(args.target)), end="")
        else:
            if not args.target:
                return _fail("import needs a YAML file", 2)
            doc = yaml.safe_load(Path(args.target).read_text())
            print(f"imported {doc.get('id')} -> {store.put(doc)}")
    except HandoffError as e:
        return _fail(str(e))
    return 0


def _cmd_run(ws: Workspace, args) -> int:
    from .agent import run_role
    from .handoffs import HandoffError, HandoffStore
    from .models import ModelConfigError
    from .tools import RequirementsError
    from .tools.human import ConsoleHuman, DeferredHuman

    try:
        store = HandoffStore(ws, args.project, args.handoffs_dir)
        task = store.get(args.task) if args.task else None
        human = ConsoleHuman() if args.human == "console" else DeferredHuman()
        say = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
        result = run_role(ws, args.project, args.role, inputs=args.input, task=task, model_id=args.model,
                          handoffs=store, human=human, expect=args.expect or None, on_event=say)
    except (ResolutionError, HandoffError, ModelConfigError, RequirementsError, KeyError) as e:
        return _fail(str(e))

    print(f"\n{result.role}: {result.stop_reason}  ({result.model_calls} model calls, {result.tool_calls} tool calls, "
          f"{result.tokens['total']} tokens)")
    for handoff_id in result.outputs:
        d = store.get(handoff_id)
        print(f"  wrote {handoff_id} [{d['status']}] {d['title']}  -> {store.path_of(handoff_id)}")
    if result.questions:
        print("  questions for you:")
        print("\n".join(f"    - {q}" for q in result.questions))
    if result.final_message:
        print(f"  agent: {result.final_message[:500]}")
    print(f"  audit log: {result.audit_path}")
    return 0 if result.ok else 1


DECISIONS = {"approve": "approved", "revise": "changes_requested", "reject": "rejected"}


def _pipeline_errors() -> tuple[type[Exception], ...]:
    from .handoffs import HandoffError
    from .models import ModelConfigError
    from .pipeline import PipelineError
    from .tools import RequirementsError

    return ResolutionError, HandoffError, ModelConfigError, RequirementsError, PipelineError, KeyError


def _pipeline(ws: Workspace, args):
    from .handoffs import HandoffStore
    from .pipeline import Pipeline

    say = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    return Pipeline(ws, args.project, HandoffStore(ws, args.project, args.handoffs_dir), model_id=args.model, on_event=say)


def _cmd_pipeline(ws: Workspace, args) -> int:
    if args.project not in ws.projects:
        return _fail(f"unknown project {args.project!r}", 2)
    p = _pipeline(ws, args)
    if args.graph:
        print(p.mermaid())
        return 0
    if not args.idea:
        return _fail("which idea? e.g. fw pipeline ridexpress idea-001", 2)
    try:
        status = p.status(args.idea) if args.status else p.run(args.idea)
    except _pipeline_errors() as e:
        print(_format_status(p.status(args.idea), args), file=sys.stderr)
        return _fail(f"{e}\nfix it, then run `fw pipeline {args.project} {args.idea}` to continue")
    print(_format_status(status, args))
    return 0 if status.state in ("waiting", "ready_for_development") else 1


def _cmd_decide(ws: Workspace, args) -> int:
    if args.project not in ws.projects:
        return _fail(f"unknown project {args.project!r}", 2)
    approvers = [a for a in ws.projects[args.project].data.get("approvers", []) if not a.startswith("<")]
    by = args.by or (approvers[0] if len(approvers) == 1 else None)
    if not by:
        return _fail("say who decides with --by (the project has no single approver)", 2)
    if approvers and by not in approvers:
        return _fail(f"{by} isn't an approver of {args.project} ({', '.join(approvers)})", 2)
    p = _pipeline(ws, args)
    try:
        doc = p.store.decide(args.handoff, DECISIONS[args.command], by=by, comment=args.comment)
        idea = p.idea_of(args.handoff)
    except _pipeline_errors() as e:
        return _fail(str(e))
    print(f"{doc['id']}: {doc['approval']['decision']} by {by}")
    if args.no_resume:
        print(f"continue with: fw pipeline {args.project} {idea}{_dir_flag(args)}")
        return 0
    args.idea = idea
    args.status = args.graph = False
    return _cmd_pipeline(ws, args)


def _format_status(s, args) -> str:
    lines = [f"pipeline {s.idea}: {s.state}" + (f" ({s.detail})" if s.detail else "")]
    for r in s.runs:
        lines.append(f"  {r['step']:<18} {r['stop_reason']:<16} {', '.join(r['outputs']) or '-':<24} "
                     f"{r['model_calls']} calls, {r['tokens']} tokens")
        lines.extend(f"    question: {q}" for q in r.get("questions", []))
    if s.docs:
        lines.append("  documents: " + "; ".join(f"{t} {', '.join(ids)}" for t, ids in s.docs.items()))
    if s.waiting_on:
        w, flag = s.waiting_on, _dir_flag(args)
        lines += [f"  waiting for approval: {w['handoff']} ({w['type']} by {w['author']}) {w['title']}",
                  f"    read it:  {w['path']}",
                  f"    then:     fw approve {args.project} {w['handoff']}{flag}",
                  f"          or: fw revise {args.project} {w['handoff']} -m \"what to change\"{flag}",
                  f"          or: fw reject {args.project} {w['handoff']} -m \"why\"{flag}"]
    return "\n".join(lines)


def _dir_flag(args) -> str:
    return f" --handoffs-dir {args.handoffs_dir}" if args.handoffs_dir else ""


def _cmd_search(resolved: Resolved, args) -> int:
    from .tools import build_toolset

    ts = build_toolset(resolved, check_requirements=False)
    try:
        tool = ts.get("rag.search")
    except KeyError as e:
        return _fail(e.args[0], 2)
    call = {"query": args.query, "k": args.k, "mmr": not args.no_mmr}
    if args.source:
        call["sources"] = args.source
    result = tool.invoke(call)
    print(result)
    return 1 if result.startswith(("Denied:", "Error:")) else 0


def _cmd_checks(resolved: Resolved, args) -> int:
    from .tools import ToolInputError, build_toolset, check_requirements

    if not resolved.verification:
        return _fail(f"role {resolved.role} has no verification checks here")
    if not args.skip_requirements:
        problems = check_requirements(resolved)
        if problems:
            return _fail("host requirements not met:\n" + "\n".join(f"  - {p}" for p in problems)
                         + "\n(use --skip-requirements to run anyway)")
    ts = build_toolset(resolved, check_requirements=False)
    wanted = args.check_ids or [c.id for c in resolved.verification]
    checks = {c.id: c for c in resolved.verification}
    unknown = [c for c in wanted if c not in checks]
    if unknown:
        return _fail(f"unknown check(s): {', '.join(unknown)} (have: {', '.join(checks)})", 2)

    results = []
    for check_id in wanted:
        try:
            results.append(ts.context.runner.run_check(checks[check_id]))
        except ToolInputError as e:
            return _fail(f"check {check_id}: {e}")
    if args.json:
        print(json.dumps([r.to_handoff() for r in results], indent=2))
    else:
        for r in results:
            print(r.format() + "\n")
    print(f"audit log: {ts.context.audit.path}", file=sys.stderr)
    return 1 if any(r.blocking and not r.passed for r in results) else 0


# ---- Helpers -------------------------------------------------------------------

def _resolve(ws: Workspace, args) -> Resolved:
    task = None
    if args.task:
        doc = ws.project_handoffs(args.project).get(args.task)
        if doc is None:
            raise LookupError(f"task {args.task!r} not found in project {args.project!r}")
        task = doc.data
    return resolve(ws, args.project, args.role, task, allow_disabled=args.allow_disabled)


def _parse_args(pairs: list[str], schema: dict) -> dict:
    props = schema.get("properties", {})
    out = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"--arg must look like NAME=VALUE, got {pair!r}")
        kind = props.get(name, {}).get("type", "string")
        if kind == "integer":
            out[name] = int(value)
        elif kind == "boolean":
            out[name] = value.lower() in ("1", "true", "yes")
        elif kind == "array":
            out[name] = [v for v in value.split(",") if v]
        else:
            out[name] = value
    return out


def load_dotenv(path: Path) -> None:
    """KEY=VALUE lines from a local .env (git-ignored) into the environment. Existing variables win."""
    import os

    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _fail(message: str, code: int = 1) -> int:
    print(f"fw: {message}", file=sys.stderr)
    return code


def format_resolved(r: Resolved, *, show_prompt: bool = False) -> str:
    p = r.permissions
    lines = [
        f"{r.role} @ {r.project}" + (f"  task {r.task} ({r.platform})" if r.task else ""),
        f"  packs:        {', '.join(r.packs) or '-'}",
        f"  model:        {r.model['provider']}/{r.model['id']}",
        f"  consumes:     {', '.join(r.consumes)}",
        f"  produces:     {', '.join(r.produces)}",
        f"  gates:        {', '.join(g['artifact'] for g in r.gates) or '-'}",
        "  limits:       " + ", ".join(f"{k}={v}" for k, v in r.limits.items()),
        f"  tools ({len(r.tools)}):    {', '.join(t['id'] for t in r.tools)}",
    ]
    if r.command_cwd:
        lines.append(f"  commands run in: {r.command_cwd}")
    if r.pack_tools:
        lines.append("  pack tools:")
        lines += [f"    {t.id}: {t.command}" for t in r.pack_tools]
    if r.verification:
        lines.append("  checks:")
        lines += [f"    {c.id}{' (blocking)' if c.blocking else ''}: {c.command}" for c in r.verification]
    lines.append("  knowledge:")
    lines += [f"    [{k.origin}] {k.id}: {k.location}" for k in r.knowledge] or ["    -"]
    lines += [
        "  permissions:",
        f"    read:     {' AND '.join(str(list(layer)) for layer in p.fs_read)}",
        f"    write:    {' AND '.join(str(list(layer)) for layer in p.fs_write)}",
        f"    deny:     {list(p.fs_deny)}",
        f"    commands: {sorted(p.commands)}",
        f"    network:  {p.network} {list(p.network_allowlist) if p.network == 'allowlist' else ''}".rstrip(),
        f"    git:      {dict(p.git)}",
    ]
    if show_prompt:
        lines += ["", "=" * 72, r.instructions]
    else:
        lines.append(f"  prompt:       {len(r.instructions)} chars (--show-prompt to print)")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

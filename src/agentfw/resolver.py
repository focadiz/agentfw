"""Resolve a role, for a project and optionally a task, into one effective configuration.

This is the only place role, pack and project configs are merged. Everything
downstream (model factory, tool factory, guard) reads a Resolved object,
never the raw YAML.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Doc, Workspace
from .permissions import Permissions
from .variables import VariableError, expand, expand_all

USE_ALL = ("instructions", "knowledge", "tools", "verification")
PROMPT_SEPARATOR = "\n\n---\n\n"


class ResolutionError(Exception):
    pass


@dataclass(frozen=True)
class Knowledge:
    origin: str                 # "role" or the pack id
    id: str
    type: str
    location: str
    description: str = ""


@dataclass(frozen=True)
class PackTool:
    pack: str
    id: str
    description: str
    command: str                # ${args.*} still unexpanded
    args: Mapping[str, Any]
    timeout_seconds: int | None


@dataclass(frozen=True)
class Check:
    pack: str
    id: str
    description: str
    command: str
    blocking: bool
    success: Mapping[str, Any]
    artifacts: tuple[str, ...]
    timeout_seconds: int | None


@dataclass(frozen=True)
class Resolved:
    project: str
    project_root: str
    role: str
    task: str | None
    platform: str | None
    packs: tuple[str, ...]
    model: Mapping[str, Any]
    instructions: str
    tools: tuple[Mapping[str, Any], ...]     # tool registry entries
    pack_tools: tuple[PackTool, ...]
    knowledge: tuple[Knowledge, ...]
    verification: tuple[Check, ...]
    permissions: Permissions
    gates: tuple[Mapping[str, Any], ...]
    limits: Mapping[str, Any]
    memory: Mapping[str, Any]
    requirements: Mapping[str, Mapping[str, Any]]   # by pack id, only for packs whose commands run
    command_cwd: str | None     # where pack tools and checks run; None if the role runs none
    workdir: str | None         # the task's scratch dir, outside the repos
    consumes: tuple[str, ...]
    produces: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["permissions"] = self.permissions.to_dict()
        return d


def agentfw_home() -> Path:
    """Where the framework keeps runtime state (scratch dirs, logs). Override with AGENTFW_HOME."""
    return Path(os.environ.get("AGENTFW_HOME", Path.home() / ".agentfw"))


def default_work_root() -> Path:
    return Path(os.environ.get("AGENTFW_WORK", agentfw_home() / "work"))


def resolve(
    ws: Workspace,
    project_id: str,
    role_id: str,
    task: Mapping[str, Any] | None = None,
    *,
    work_root: Path | None = None,
    allow_disabled: bool = False,
) -> Resolved:
    """Merge project + role + selected packs for one role run.

    task is a full task handoff document. Roles that pick their pack by
    task.platform (Developer, QA, DevOps) require one.
    """
    project = _lookup(ws.projects, project_id, "project").data
    role_doc = _lookup(ws.roles, role_id, "role")
    role = role_doc.data

    entry = next((r for r in project["roles"] if r["id"] == role_id), None)
    if entry is None:
        raise ResolutionError(f"role {role_id!r} isn't part of project {project_id!r}")
    if not entry.get("enabled", True) and not allow_disabled:
        raise ResolutionError(f"role {role_id!r} is disabled in project {project_id!r}")
    if task is not None and task.get("type") != "task":
        raise ResolutionError(f"{task.get('id')!r} is a {task.get('type')!r}, not a task")
    task_body = task["body"] if task else None

    ctx = _base_context(project, role, task, work_root or default_work_root())
    packs = _select_packs(ws, project, role, task_body)
    use = tuple(role.get("skill_packs", {}).get("use", USE_ALL))

    model = dict(role["model"])
    if entry.get("model_id"):
        model["id"] = entry["model_id"]

    instructions = [_expand(_read(role_doc, role["instructions"]), ctx, "role instructions")]
    tools = _registry_tools(ws, role)
    knowledge = [
        Knowledge("role", k["id"], k["type"], _expand(k["location"], ctx, f"role knowledge {k['id']}"), k.get("description", ""))
        for k in role.get("knowledge", [])
    ]
    permissions = Permissions.from_role(_expand(role["permissions"], ctx, "role permissions"))
    pack_tools: list[PackTool] = []
    checks: list[Check] = []
    requirements: dict[str, Mapping[str, Any]] = {}
    command_cwd: str | None = None
    run_in = role.get("skill_packs", {}).get("run_in", "repo")

    for pack_doc, platform in packs:
        pack = pack_doc.data
        platform_values = _platform_values(project, platform)
        # The worktree is where this role works on the platform's code: the repo
        # itself, or QA's clean checkout. Pack commands and write globs use it.
        if run_in == "checkout":
            if "task" not in ctx:
                raise ResolutionError(f"role {role_id!r} works in a checkout; a task is required")
            platform_values["worktree"] = ctx["task"]["checkout"]
        else:
            platform_values["worktree"] = platform_values["repo_root"]
        pctx = {**ctx, "platform": platform_values, "pack": _pack_values(project, pack, platform, task_body)}
        where = f"pack {pack['id']}"

        if "instructions" in use:
            instructions.append(_expand(_read(pack_doc, pack["instructions"]), pctx, f"{where} instructions"))
        if "knowledge" in use:
            knowledge += [
                Knowledge(pack["id"], k["id"], k["type"], _expand(k["location"], pctx, f"{where} knowledge {k['id']}"), k.get("description", ""))
                for k in pack.get("knowledge", [])
            ]
        if "tools" in use:
            pack_tools += [
                PackTool(pack["id"], t["id"], t["description"], _expand(t["command"], pctx, f"{where} tool {t['id']}"),
                         t.get("args", {}), t.get("timeout_seconds"))
                for t in pack.get("tools", [])
            ]
        if "verification" in use:
            checks += [
                Check(pack["id"], c["id"], c.get("description", ""), _expand(c["command"], pctx, f"{where} check {c['id']}"),
                      c["blocking"], c.get("success", {}),
                      tuple(_expand(c.get("artifacts", []), pctx, f"{where} check {c['id']} artifacts")),
                      c.get("timeout_seconds"))
                for c in pack["verification"]
            ]
        # Pack permissions only matter when the pack's tools or checks run.
        if {"tools", "verification"} & set(use):
            requirements[pack["id"]] = pack.get("requirements", {})
            if "permissions" in pack:
                permissions = permissions.narrow(_expand(pack["permissions"], pctx, f"{where} permissions"))
            command_cwd = platform_values["worktree"]

    return Resolved(
        project=project_id,
        project_root=project["root"],
        role=role_id,
        task=task["id"] if task else None,
        platform=task_body["platform"] if task_body else None,
        packs=tuple(p.data["id"] for p, _ in packs),
        model=model,
        instructions=PROMPT_SEPARATOR.join(instructions),
        tools=tools,
        pack_tools=tuple(pack_tools),
        knowledge=tuple(knowledge),
        verification=tuple(checks),
        permissions=permissions,
        gates=tuple(role.get("gates", [])),
        limits=dict(role["limits"]),
        memory=dict(role.get("memory", {})),
        requirements=requirements,
        command_cwd=command_cwd,
        workdir=ctx["task"]["workdir"] if "task" in ctx else None,
        consumes=tuple(role["consumes"]),
        produces=tuple(role["produces"]),
    )


# ---- Context ---------------------------------------------------------------

def _base_context(project: Mapping, role: Mapping, task: Mapping | None, work_root: Path) -> dict[str, dict[str, str]]:
    root = Path(project["root"])
    # Optional paths are only defined when set, so a missing docs_root fails
    # loudly instead of expanding "${project.docs_root}/**" to "/**".
    project_values = {
        "id": project["id"],
        "name": project["name"],
        "root": str(root),
        "handoffs_dir": project["handoffs_dir"],
        "platforms": ", ".join(project["platforms"]),
    }
    if "docs_root" in project:
        project_values["docs_root"] = project["docs_root"]

    ctx = {
        "project": project_values,
        "role": {"id": role["id"], "branch_prefix": role["permissions"].get("git", {}).get("branch_prefix", "")},
    }
    if task:
        body = task["body"]
        repo = project["repos"].get(body["repo"])
        if repo is None:
            raise ResolutionError(f"task {task['id']}: repo {body['repo']!r} isn't in the project")
        ctx["task"] = {
            "id": task["id"],
            "platform": body["platform"],
            "repo": body["repo"],
            "repo_root": str(root / repo["path"]),
            "workdir": str(Path(work_root) / project["id"] / task["id"]),
            "checkout": str(Path(work_root) / project["id"] / task["id"] / "checkout"),
        }
    return ctx


def _platform_values(project: Mapping, platform: str) -> dict[str, str]:
    plat = project["platforms"][platform]
    repo = project["repos"][plat["repo"]]
    values = {"name": platform, "repo": plat["repo"], "repo_root": str(Path(project["root"]) / repo["path"])}
    if "remote" in repo:
        values["repo_remote"] = repo["remote"]
    return values


def _pack_values(project: Mapping, pack: Mapping, platform: str, task_body: Mapping | None) -> dict[str, str]:
    """Defaults, then project platform values, then the task's pack_variables. Only declared variables."""
    declared = pack.get("variables", {})
    values = {name: v["default"] for name, v in declared.items() if "default" in v}
    values.update({k: v for k, v in project["platforms"][platform].get("variables", {}).items() if k in declared})
    if task_body and task_body["platform"] == platform:
        values.update({k: v for k, v in task_body.get("pack_variables", {}).items() if k in declared})
    return values


# ---- Pack selection ----------------------------------------------------------

def _select_packs(ws: Workspace, project: Mapping, role: Mapping, task_body: Mapping | None) -> list[tuple[Doc, str]]:
    sp = role.get("skill_packs")
    if not sp:
        return []
    allowed = sp.get("allowed")
    packs = ws.packs

    def eligible(pack_id: str | None) -> bool:
        return (
            pack_id in packs
            and role["id"] in packs[pack_id].data["applies_to"]
            and (allowed is None or pack_id in allowed)
        )

    if sp["select_by"] == "task.platform":
        if task_body is None:
            raise ResolutionError(f"role {role['id']!r} picks its pack from the task's platform; a task is required")
        platform = task_body["platform"]
        plat = project["platforms"].get(platform)
        if plat is None:
            raise ResolutionError(f"platform {platform!r} isn't defined in project {project['id']!r}")
        # A platform has a build pack and optionally a deploy pack; the role's
        # applies_to decides which one it gets (DevOps gets the deploy pack).
        matches = [p for p in (plat.get("pack"), plat.get("deploy_pack")) if eligible(p)]
        if matches:
            return [(packs[matches[0]], platform)]
        if sp["mode"] == "required":
            raise ResolutionError(f"no skill pack for role {role['id']!r} on platform {platform!r}")
        return []

    selected = [(packs[p["pack"]], name) for name, p in project["platforms"].items() if eligible(p.get("pack"))]
    if not selected and sp["mode"] == "required":
        raise ResolutionError(f"no skill packs for role {role['id']!r} in project {project['id']!r}")
    return selected


# ---- Helpers ---------------------------------------------------------------

def _lookup(items: Mapping[str, Doc], key: str, what: str) -> Doc:
    if key not in items:
        raise ResolutionError(f"unknown {what} {key!r}")
    return items[key]


def _read(doc: Doc, relative: str) -> str:
    path = doc.path.parent / relative
    if not path.is_file():
        raise ResolutionError(f"{doc.path.name}: instructions file not found: {relative}")
    return path.read_text()


def _expand(value: Any, ctx: Mapping, where: str) -> Any:
    try:
        return expand(value, ctx) if isinstance(value, str) else expand_all(value, ctx)
    except VariableError as e:
        raise ResolutionError(f"{where}: {e}") from None


def _registry_tools(ws: Workspace, role: Mapping) -> tuple[Mapping[str, Any], ...]:
    registry = {t["id"]: t for t in ws.tool_registry.data["tools"]} if ws.tool_registry else {}
    missing = [t for t in role.get("tools", []) if t not in registry]
    if missing:
        raise ResolutionError(f"role {role['id']!r}: tools not in the registry: {missing}")
    return tuple(registry[t] for t in role.get("tools", []))

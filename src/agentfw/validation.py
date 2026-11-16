"""Validate framework configs and handoff documents.

Each YAML file is checked against its JSON Schema, then the references
between files that a schema can't express: tools exist, packs fit within
their role's permission ceiling, the pipeline is wired end to end, tasks
point at real checks, designs cover every requirement, and every enabled
role resolves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .config import Doc, Workspace
from .permissions import LEVELS
from .resolver import ResolutionError, resolve
from .variables import NAMESPACES, VAR


@dataclass
class Report:
    root: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)     # error messages without the location prefix

    def error(self, where: Path | str, msg: str) -> None:
        self.errors.append(f"ERROR {self._rel(where)}: {msg}")
        self.messages.append(msg)

    def warn(self, where: Path | str, msg: str) -> None:
        self.warnings.append(f"WARN  {self._rel(where)}: {msg}")

    def format(self) -> str:
        lines = self.errors + self.warnings
        if self.checked:
            lines.append("\nChecked " + ", ".join(f"{n} {k}" for k, n in self.checked.items()) + ".")
        lines.append(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s).")
        return "\n".join(lines)

    def _rel(self, where: Path | str) -> str:
        if isinstance(where, Path):
            try:
                return str(where.relative_to(self.root))
            except ValueError:
                return str(where)
        return where


def validate(ws: Workspace) -> Report:
    report = Report(ws.root)
    for path, msg in ws.load_errors:
        report.error(path, msg)
    _check_schemas(ws, report)
    if report.errors:        # cross-reference checks assume schema-valid documents
        return report
    registry_ids = _check_registry(ws, report)
    _check_packs(ws, report, registry_ids)
    _check_roles(ws, report, registry_ids)
    _check_projects(ws, report)
    _check_handoffs(ws, report)
    if not report.errors:    # resolution assumes consistent references
        _check_resolution(ws, report)
    report.checked = {kind: len(docs) for kind, docs in ws.docs.items()}
    return report


# ---- Schemas ---------------------------------------------------------------

def _check_schemas(ws: Workspace, report: Report) -> None:
    for kind, docs in ws.docs.items():
        validator = Draft202012Validator(ws.schemas[kind], registry=ws.schema_registry, format_checker=FormatChecker())
        for doc in docs:
            for e in sorted(validator.iter_errors(doc.data), key=lambda e: list(e.absolute_path)):
                loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
                report.error(doc.path, f"schema: {loc}: {e.message}")


# ---- Tool registry -----------------------------------------------------------

def _check_registry(ws: Workspace, report: Report) -> set[str]:
    ids: set[str] = set()
    if not ws.docs["tool_registry"]:
        report.error("tools/", "no tool registry found")
    if len(ws.docs["tool_registry"]) > 1:
        report.error("tools/", "more than one tool registry")
    for doc in ws.docs["tool_registry"]:
        servers = set(doc.data.get("mcp_servers", {}))
        for tool in doc.data["tools"]:
            if tool["id"] in ids:
                report.error(doc.path, f"duplicate tool id {tool['id']}")
            ids.add(tool["id"])
            server = tool["impl"].get("mcp_server")
            if server and server not in servers:
                report.error(doc.path, f"tool {tool['id']} uses undefined MCP server {server!r}")
            if "none" in tool["guarded_by"] and tool["effect"] == "remote":
                report.error(doc.path, f"tool {tool['id']} has remote effects but guarded_by: none")
    return ids


# ---- Packs -----------------------------------------------------------------

def _check_var_refs(report: Report, path: Path, where: str, text: str, declared: set[str], args: set[str] | None = None) -> None:
    for ns, name in VAR.findall(text):
        if ns not in NAMESPACES:
            report.error(path, f"{where}: unknown variable namespace ${{{ns}.{name}}}")
        elif ns == "pack" and name not in declared:
            report.error(path, f"{where}: ${{pack.{name}}} is not declared in the pack's variables")
        elif ns == "args" and (args is None or name not in args):
            report.error(path, f"{where}: ${{args.{name}}} is not a declared argument")


def _check_packs(ws: Workspace, report: Report, registry_ids: set[str]) -> None:
    roles = set(ws.roles)
    _check_duplicates(ws.docs["skill_pack"], report, "pack")
    for doc in ws.docs["skill_pack"]:
        pack, path = doc.data, doc.path
        if not (path.parent / pack["instructions"]).is_file():
            report.error(path, f"instructions file not found: {pack['instructions']}")
        for r in pack["applies_to"]:
            if r not in roles:
                report.error(path, f"applies_to unknown role {r!r}")
        declared = set(pack.get("variables", {}))
        for tool in pack.get("tools", []):
            if tool["id"] in registry_ids:
                report.error(path, f"pack tool {tool['id']} collides with a registry tool")
            _check_var_refs(report, path, f"tool {tool['id']}", tool["command"], declared, set(tool.get("args", {})))
            for name, arg in tool.get("args", {}).items():
                if "pattern" not in arg and arg["type"] == "string":
                    report.warn(path, f"tool {tool['id']}: string arg {name!r} has no pattern")
        for check in pack["verification"]:
            _check_var_refs(report, path, f"check {check['id']}", check["command"], declared)
            for artifact in check.get("artifacts", []):
                _check_var_refs(report, path, f"check {check['id']} artifacts", artifact, declared)
            pattern = check.get("success", {}).get("output_must_not_match")
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as e:
                    report.error(path, f"check {check['id']}: invalid regex: {e}")
        for k in pack.get("knowledge", []):
            _check_var_refs(report, path, f"knowledge {k['id']}", k["location"], declared)
        for fld in ("fs_read", "fs_write", "fs_deny"):
            for glob in pack.get("permissions", {}).get(fld, []):
                _check_var_refs(report, path, f"permissions.{fld}", glob, declared)


# ---- Roles -----------------------------------------------------------------

def _check_roles(ws: Workspace, report: Report, registry_ids: set[str]) -> None:
    packs = ws.packs
    _check_duplicates(ws.docs["role"], report, "role")
    for doc in ws.docs["role"]:
        role, path = doc.data, doc.path
        if not (path.parent / role["instructions"]).is_file():
            report.error(path, f"instructions file not found: {role['instructions']}")
        for t in role.get("tools", []):
            if t not in registry_ids:
                report.error(path, f"tool {t!r} is not in the tool registry")
        for g in role.get("gates", []):
            if g["artifact"] not in role["produces"]:
                report.error(path, f"gate on {g['artifact']!r}, which this role doesn't produce")
        sp = role.get("skill_packs")
        if not sp:
            continue
        candidates = sp.get("allowed") or [pid for pid, p in packs.items() if role["id"] in p.data["applies_to"]]
        executes = not sp.get("use") or bool({"tools", "verification"} & set(sp["use"]))
        for pid in candidates:
            if pid not in packs:
                report.warn(path, f"allowed pack {pid!r} doesn't exist yet")
                continue
            pack_doc = packs[pid]
            if role["id"] not in pack_doc.data["applies_to"]:
                report.error(path, f"allows pack {pid!r}, but the pack's applies_to doesn't include {role['id']!r}")
            if executes:
                _check_ceiling(report, role, pack_doc)


def _check_ceiling(report: Report, role: dict, pack_doc: Doc) -> None:
    """A pack's permission request must fit inside the role's ceiling."""
    rp, pp, path = role["permissions"], pack_doc.data.get("permissions", {}), pack_doc.path
    extra = set(pp.get("commands_allow", [])) - set(rp.get("commands_allow", []))
    if extra:
        report.error(path, f"requests commands {sorted(extra)} outside role {role['id']!r} ceiling")
    if LEVELS[pp.get("network", "none")] > LEVELS[rp.get("network", "none")]:
        report.error(path, f"requests network {pp['network']!r}, above role {role['id']!r} ceiling")
    if rp.get("network") == "allowlist":
        for host in pp.get("network_allowlist", []):
            if not any(fnmatchcase(host, r) for r in rp.get("network_allowlist", [])):
                report.error(path, f"requests host {host!r} outside role {role['id']!r} allowlist")
    # Every command the pack runs must be in the pack's own allowlist.
    allowed = set(pp.get("commands_allow", []))
    for item in pack_doc.data.get("tools", []) + pack_doc.data["verification"]:
        exe = item["command"].split()[0]
        if exe not in allowed:
            report.error(path, f"{item['id']} runs {exe!r}, which the pack's commands_allow doesn't include")


# ---- Projects --------------------------------------------------------------

def _check_projects(ws: Workspace, report: Report) -> None:
    packs, roles = ws.packs, ws.roles
    _check_duplicates(ws.docs["project"], report, "project")
    for doc in ws.docs["project"]:
        proj, path = doc.data, doc.path
        for r in proj["roles"]:
            if r["id"] not in roles:
                report.error(path, f"role {r['id']!r} doesn't exist")
        for name, plat in proj["platforms"].items():
            if plat["repo"] not in proj["repos"]:
                report.error(path, f"platform {name}: repo {plat['repo']!r} not in repos")
            given = plat.get("variables", {})
            platform_packs = [packs[p].data for k in ("pack", "deploy_pack") if (p := plat.get(k)) in packs]
            declared = {v for pack in platform_packs for v in pack.get("variables", {})}
            for vname, value in given.items():
                for ns, used in VAR.findall(value):
                    if ns == "pack" and used not in declared:
                        report.error(path, f"platform {name}: {vname} references undeclared ${{pack.{used}}}")
            needed: set[str] = set()
            for key in ("pack", "deploy_pack"):
                pid = plat.get(key)
                if not pid:
                    continue
                if pid not in packs:
                    report.error(path, f"platform {name}: {key} {pid!r} doesn't exist")
                    continue
                pack = packs[pid].data
                if pack["platform"] != name:
                    report.error(path, f"platform {name}: {key} {pid!r} is for platform {pack['platform']!r}")
                for vname, v in pack.get("variables", {}).items():
                    if v.get("scope", "project") == "project":
                        needed.add(vname)
                        if vname not in given and "default" not in v:
                            report.error(path, f"platform {name}: pack {pid} needs variable {vname!r}")
            for vname in set(given) - needed:
                report.warn(path, f"platform {name}: variable {vname!r} isn't a project-scoped variable of its packs")
        enabled = {r["id"] for r in proj["roles"] if r.get("enabled", True)}
        _check_pipeline(ws, report, path, enabled)


def _check_pipeline(ws: Workspace, report: Report, project_path: Path, enabled: set[str]) -> None:
    roles = {rid: d.data for rid, d in ws.roles.items() if rid in enabled}
    produced = {t for r in roles.values() for t in r["produces"]} | {"idea"}   # ideas come from humans
    for rid, role in roles.items():
        for t in role["consumes"]:
            if t not in produced:
                report.error(project_path, f"role {rid} consumes {t!r}, which no enabled role produces")


# ---- Handoffs --------------------------------------------------------------

def _check_handoffs(ws: Workspace, report: Report) -> None:
    _check_duplicates(ws.docs["handoff"], report, "handoff")
    by_id = ws.by_id("handoff")
    projects = {pid: d.data for pid, d in ws.projects.items()}
    packs = ws.packs

    for doc in ws.docs["handoff"]:
        _check_one_handoff(report, doc.path, doc.data, by_id, projects, packs, require_parents=False)


def handoff_errors(ws: Workspace, data: dict, others: dict[str, dict]) -> list[str]:
    """Problems with one handoff document, checked against other documents by id.

    Used when an agent writes a document: the messages go back to the agent so
    it can fix them. Unlike repo validation, missing parents are errors here.
    """
    report = Report(ws.root)
    validator = Draft202012Validator(ws.schemas["handoff"], registry=ws.schema_registry, format_checker=FormatChecker())
    for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
        report.error("document", f"{loc}: {e.message}")
    if report.messages:
        return report.messages
    by_id = {i: Doc(Path(f"{i}.yaml"), d) for i, d in others.items()}
    projects = {pid: d.data for pid, d in ws.projects.items()}
    _check_one_handoff(report, Path("document"), data, by_id, projects, ws.packs, require_parents=True)
    return report.messages


def _check_one_handoff(report: Report, path: Path, h: dict, by_id: dict[str, Doc], projects: dict, packs: dict,
                       *, require_parents: bool) -> None:
    body, htype = h["body"], h["type"]
    if not h["id"].startswith(htype + "-"):
        report.error(path, f"id {h['id']!r} doesn't match type {htype!r}")
    for parent in h.get("parents", []):
        if parent not in by_id:
            (report.error if require_parents else report.warn)(path, f"parent {parent!r} not found")
    proj = projects.get(h["project"])
    if not proj:
        report.error(path, f"unknown project {h['project']!r}")
        return

    if htype == "spec":
        _check_spec(report, path, h, by_id)
    elif htype == "task_list":
        _check_task_list(report, path, h, by_id)
    elif htype == "task":
        _check_task(report, path, body, proj, packs)
    elif htype == "design":
        _check_design(report, path, h, by_id)
    if htype in ("pull_request", "test_report") and body["task_id"] not in h.get("parents", []):
        report.error(path, f"task_id {body['task_id']} should be listed in parents")
    if htype == "test_report":
        _check_test_report(report, path, body, by_id)


def _parent_of_type(h: dict, by_id: dict[str, Doc], htype: str) -> dict | None:
    return next((by_id[p].data for p in h.get("parents", []) if p in by_id and by_id[p].data["type"] == htype), None)


# Design details a spec's requirements shouldn't decide: named APIs, endpoints, HTTP statuses.
DESIGN_TERMS = re.compile(
    r"\b[a-z0-9]+(?:-[a-z0-9]+)*-api\b"                          # mobile-experience-api
    r"|\b(?:experience|process|system)\s+APIs?\b"                # the API layers
    r"|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[\w/{}.:-]*"            # POST /users
    r"|\b(?:HTTP|status(?:\s+code)?)\s*[1-5]\d\d\b",               # HTTP 409
    re.IGNORECASE,
)


def _check_spec(report: Report, path: Path, h: dict, by_id: dict[str, Doc]) -> None:
    seen = set()
    for r in h["body"]["requirements"]:
        if r["id"] in seen:
            report.error(path, f"requirement id {r['id']} is used twice")
        seen.add(r["id"])
    # A term the idea itself uses (e.g. a contract in its constraints) may appear.
    idea = _parent_of_type(h, by_id, "idea")
    allowed = " ".join(idea["body"].get("goals", []) + idea["body"].get("constraints", [])).lower() if idea else ""
    for r in h["body"]["requirements"]:
        texts = [r["statement"], *r.get("acceptance_criteria", [])]
        found = dict.fromkeys(m.group().strip().rstrip(".:") for t in texts for m in DESIGN_TERMS.finditer(t))
        named = [t for t in found if t.lower() not in allowed]
        if named:
            report.error(path, f"{r['id']} decides design: {', '.join(repr(t) for t in named)}. Say what the user "
                               "or system does and leave APIs, endpoints and status codes to the architect; only "
                               "terms in the idea's goals or constraints may appear. A design question goes in "
                               "open_questions.")


def _check_task_list(report: Report, path: Path, h: dict, by_id: dict[str, Doc]) -> None:
    stories = h["body"]["stories"]
    story_ids = [s["id"] for s in stories]
    for sid in {s for s in story_ids if story_ids.count(s) > 1}:
        report.error(path, f"story id {sid} is used twice")
    for s in stories:
        for dep in s.get("depends_on", []):
            if dep not in story_ids:
                report.error(path, f"{s['id']} depends on unknown story {dep}")
    spec = _parent_of_type(h, by_id, "spec")
    if not spec:
        report.warn(path, "no parent spec found; requirement references not checked")
        return
    req_ids = {r["id"] for r in spec["body"]["requirements"]}
    for s in stories:
        for rid in s["requirement_ids"]:
            if rid not in req_ids:
                report.error(path, f"{s['id']} references {rid}, which isn't in {spec['id']}")
    covered = {rid for s in stories for rid in s["requirement_ids"]}
    for r in spec["body"]["requirements"]:
        if r["priority"] in ("must", "should") and r["id"] not in covered:
            report.error(path, f"{r['priority']} requirement {r['id']} isn't in any story")


def _check_task(report: Report, path: Path, body: dict, proj: dict, packs: dict[str, Doc]) -> None:
    plat = proj["platforms"].get(body["platform"])
    if not plat:
        report.error(path, f"platform {body['platform']!r} not in project")
        return
    if body["repo"] != plat["repo"]:
        report.error(path, f"repo {body['repo']!r} doesn't match platform repo {plat['repo']!r}")
    pack_doc = packs.get(plat["pack"])
    if not pack_doc:
        return
    pack = pack_doc.data
    check_ids = {c["id"] for c in pack["verification"]}
    for c in body["required_checks"]:
        if c not in check_ids:
            report.error(path, f"required check {c!r} isn't in pack {pack['id']}")
    for vname, v in pack.get("variables", {}).items():
        if v.get("scope") == "task" and vname not in body.get("pack_variables", {}) and "default" not in v:
            report.error(path, f"pack {pack['id']} needs task variable {vname!r}")


def _check_design(report: Report, path: Path, h: dict, by_id: dict[str, Doc]) -> None:
    body = h["body"]
    cmp_ids = {c["id"] for c in body["components"]}
    for req, cmps in body["requirement_coverage"].items():
        for c in cmps:
            if c not in cmp_ids:
                report.error(path, f"coverage {req} -> unknown component {c}")
    for i in body.get("interfaces", []):
        for end in (i["from"], i["to"]):
            if end not in cmp_ids:
                report.error(path, f"interface references unknown component {end}")
    spec = _parent_of_type(h, by_id, "spec")
    if not spec:
        report.warn(path, "no parent spec found; requirement coverage not checked")
        return
    for r in spec["body"]["requirements"]:
        if r["priority"] in ("must", "should") and r["id"] not in body["requirement_coverage"]:
            report.error(path, f"{r['priority']} requirement {r['id']} isn't covered")


def _check_test_report(report: Report, path: Path, body: dict, by_id: dict[str, Doc]) -> None:
    blocking_failed = any(c.get("blocking", True) and not c["passed"] for c in body["checks"])
    criterion_failed = any(a["status"] == "fail" for a in body["acceptance"])
    expected = "fail" if blocking_failed or criterion_failed else "pass"
    if body["verdict"] != expected:
        report.error(path, f"verdict {body['verdict']!r} is inconsistent with results (expected {expected!r})")
    task = by_id.get(body["task_id"])
    if task:
        ran = {c["check_id"] for c in body["checks"]}
        for c in task.data["body"]["required_checks"]:
            if c not in ran:
                report.error(path, f"required check {c!r} from {body['task_id']} wasn't run")


# ---- Resolution ------------------------------------------------------------

def _check_resolution(ws: Workspace, report: Report) -> None:
    """Every enabled role must resolve: against each example task if it needs one, otherwise on its own."""
    for pid, pdoc in ws.projects.items():
        tasks = [d for d in ws.project_handoffs(pid).values() if d.data["type"] == "task"]
        for entry in pdoc.data["roles"]:
            if not entry.get("enabled", True):
                continue
            role = ws.roles[entry["id"]].data
            needs_task = role.get("skill_packs", {}).get("select_by") == "task.platform"
            targets: list[Doc | None] = tasks if needs_task else [None]
            if needs_task and not tasks:
                report.warn(pdoc.path, f"role {entry['id']} needs a task to resolve; no tasks found")
            for task_doc in targets:
                try:
                    resolve(ws, pid, entry["id"], task_doc.data if task_doc else None)
                except ResolutionError as e:
                    where = task_doc.path if task_doc else pdoc.path
                    report.error(where, f"resolving role {entry['id']}: {e}")


def _check_duplicates(docs: list[Doc], report: Report, what: str) -> None:
    seen: dict[str, Any] = {}
    for doc in docs:
        i = doc.data["id"]
        if i in seen:
            report.error(doc.path, f"duplicate {what} id {i!r} (also in {report._rel(seen[i])})")
        seen[i] = doc.path

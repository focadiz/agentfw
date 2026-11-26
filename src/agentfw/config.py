"""Load framework files: schemas, roles, skill packs, projects, the tool registry and handoffs."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from referencing import Registry, Resource

SCHEMA_FILES = {
    "role": "role.schema.json",
    "skill_pack": "skill-pack.schema.json",
    "project": "project.schema.json",
    "tool_registry": "tool-registry.schema.json",
    "handoff": "handoff.schema.json",
}
# Directories never scanned for config files.
SKIP_DIRS = {".venv", ".work", "eval", ".git", "node_modules", "__pycache__", ".pytest_cache"}


@dataclass(frozen=True)
class Doc:
    path: Path
    data: dict


@dataclass
class Workspace:
    root: Path
    schemas: dict[str, dict]
    schema_registry: Registry
    docs: dict[str, list[Doc]]               # by kind, in file order
    load_errors: list[tuple[Path, str]]

    def by_id(self, kind: str) -> dict[str, Doc]:
        return {d.data["id"]: d for d in self.docs[kind] if isinstance(d.data.get("id"), str)}

    @property
    def roles(self) -> dict[str, Doc]:
        return self.by_id("role")

    @property
    def packs(self) -> dict[str, Doc]:
        return self.by_id("skill_pack")

    @property
    def projects(self) -> dict[str, Doc]:
        return self.by_id("project")

    @property
    def tool_registry(self) -> Doc | None:
        return self.docs["tool_registry"][0] if self.docs["tool_registry"] else None

    def project_handoffs(self, project_id: str) -> dict[str, Doc]:
        """Handoffs for a project: those under the framework root (examples) plus the project's handoffs_dir."""
        found = {d.data["id"]: d for d in self.docs["handoff"] if d.data.get("project") == project_id}
        project = self.projects.get(project_id)
        if project:
            handoffs_dir = Path(project.data["handoffs_dir"])
            if handoffs_dir.is_dir():
                for doc in load_dir(handoffs_dir, kind="handoff"):
                    found.setdefault(doc.data["id"], doc)
        return found


def find_root(start: Path | None = None) -> Path:
    """Locate the framework root: $AGENTFW_ROOT, then the nearest parent with schema/, then the package's own checkout."""
    if env := os.environ.get("AGENTFW_ROOT"):
        return Path(env).resolve()
    here = Path(start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "schema" / SCHEMA_FILES["role"]).is_file():
            return candidate
    package_root = Path(__file__).resolve().parents[2]
    if (package_root / "schema" / SCHEMA_FILES["role"]).is_file():
        return package_root
    raise FileNotFoundError("framework root not found; pass --root or set AGENTFW_ROOT")


def load_workspace(root: Path) -> Workspace:
    root = Path(root).resolve()
    schemas = {kind: json.loads((root / "schema" / name).read_text()) for kind, name in SCHEMA_FILES.items()}
    resources = []
    for kind, schema in schemas.items():
        resource = Resource.from_contents(schema)
        resources += [(schema["$id"], resource), (SCHEMA_FILES[kind], resource)]

    docs: dict[str, list[Doc]] = {kind: [] for kind in SCHEMA_FILES}
    errors: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.yaml")):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as e:
            errors.append((path, f"invalid YAML: {e}"))
            continue
        kind = data.get("kind") if isinstance(data, dict) else None
        if kind not in docs:
            errors.append((path, f"unknown or missing kind: {kind!r}"))
            continue
        if kind == "project":
            _resolve_project_paths(data, root)
        docs[kind].append(Doc(path, data))
    return Workspace(root, schemas, Registry().with_resources(resources), docs, errors)


PROJECT_PATHS = ("root", "docs_root", "handoffs_dir")


def _resolve_project_paths(data: dict, root: Path) -> None:
    """Project folders may use ~, $VARS, or a path relative to the framework root; store them absolute."""
    for key in PROJECT_PATHS:
        if isinstance(data.get(key), str):
            p = Path(os.path.expanduser(os.path.expandvars(data[key])))
            data[key] = str(p if p.is_absolute() else (root / p).resolve())


def load_dir(directory: Path, kind: str) -> list[Doc]:
    """Load every YAML file of one kind from a directory (non-recursive)."""
    out = []
    for path in sorted(Path(directory).glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        if isinstance(data, dict) and data.get("kind") == kind:
            out.append(Doc(path, data))
    return out

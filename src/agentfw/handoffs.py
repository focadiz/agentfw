"""The handoff store: where documents passed between roles live, one YAML file per document.

Agents never write the envelope themselves. They supply type, title, parents
and body; the store assigns the id, records who wrote it and when, sets the
status from the role's gates, validates the whole document (schema plus
cross-references like requirement coverage), and only then writes the file.
Invalid documents are rejected with messages the agent can act on.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import Workspace, load_dir
from .resolver import Resolved
from .validation import handoff_errors

SCHEMA_VERSION = "0.1"
# Ungated documents are accepted as written; reports are simply finished.
REPORT_TYPES = {"pull_request", "test_report", "deploy_report"}
ID = re.compile(r"^(?P<type>[a-z_]+)-(?P<n>[0-9]+)$")


class HandoffError(Exception):
    """A document was rejected. The message lists what to fix."""


class HandoffStore:
    def __init__(self, ws: Workspace, project_id: str, directory: Path | None = None):
        self.ws = ws
        self.project_id = project_id
        project = ws.projects[project_id].data
        self.dir = Path(directory or project["handoffs_dir"])

    # ---- Read ------------------------------------------------------------------

    def all(self) -> dict[str, dict]:
        if not self.dir.is_dir():
            return {}
        return {d.data["id"]: d.data for d in load_dir(self.dir, kind="handoff") if d.data.get("project") == self.project_id}

    def get(self, handoff_id: str) -> dict:
        docs = self.all()
        if handoff_id not in docs:
            raise HandoffError(f"no handoff {handoff_id!r} in {self.dir}")
        return docs[handoff_id]

    def list(self, *, type: str | None = None, status: str | None = None) -> list[dict]:   # noqa: A002
        return [d for _, d in sorted(self.all().items())
                if (type is None or d["type"] == type) and (status is None or d["status"] == status)]

    def path_of(self, handoff_id: str) -> Path:
        return self.dir / f"{handoff_id}.yaml"

    # ---- Write -----------------------------------------------------------------

    def next_id(self, htype: str) -> str:
        numbers = [int(m["n"]) for i in self.all() if (m := ID.match(i)) and m["type"] == htype]
        return f"{htype}-{max(numbers, default=0) + 1:03d}"

    def write(self, document: Mapping[str, Any], *, resolved: Resolved, run_id: str) -> dict:
        """Write a document from an agent. Revising is allowed for the role's own unapproved documents."""
        htype = document.get("type")
        if htype not in resolved.produces:
            raise HandoffError(f"role {resolved.role} can't write {htype!r} documents; it produces {list(resolved.produces)}")
        existing = self.all()
        revising = document.get("id")
        if revising:
            prior = existing.get(revising)
            if prior is None:
                raise HandoffError(f"can't revise {revising!r}: it doesn't exist (omit id to create a new document)")
            if prior["created_by"]["actor"] != resolved.role or prior["status"] in ("approved", "done", "superseded"):
                raise HandoffError(f"can't revise {revising!r}: only the author can revise it, and only before approval")
            if prior["type"] != htype:
                raise HandoffError(f"can't change {revising!r} from {prior['type']} to {htype}")

        gated = any(g["artifact"] == htype for g in resolved.gates)
        created_by = {"actor": resolved.role, "run_id": run_id}
        if resolved.packs:
            created_by["pack"] = resolved.packs[0]
        doc = {
            "kind": "handoff",
            "schema_version": SCHEMA_VERSION,
            "id": revising or self.next_id(htype),
            "type": htype,
            "project": self.project_id,
            "title": document.get("title", ""),
            "created_by": created_by,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "parents": list(document.get("parents", [])),
            "status": "pending_approval" if gated else ("done" if htype in REPORT_TYPES else "approved"),
            "body": document.get("body", {}),
        }
        others = {i: d for i, d in existing.items() if i != doc["id"]}
        problems = handoff_errors(self.ws, doc, others)
        if problems:
            raise HandoffError("document rejected:\n" + "\n".join(f"  - {p}" for p in problems))

        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_of(doc["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(dump(doc))
        tmp.replace(path)
        return doc

    def put(self, doc: Mapping[str, Any]) -> Path:
        """Store a complete document as-is (e.g. an idea written by a human). Validated, never re-enveloped."""
        others = {i: d for i, d in self.all().items() if i != doc.get("id")}
        problems = handoff_errors(self.ws, dict(doc), others)
        if problems:
            raise HandoffError("document rejected:\n" + "\n".join(f"  - {p}" for p in problems))
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_of(doc["id"])
        path.write_text(dump(dict(doc)))
        return path


def dump(doc: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True, width=100)

"""The per-project knowledge index: one Chroma collection, synced from the roles' knowledge sources.

Layout: <AGENTFW_HOME>/index/<project>/
  chroma/          the vector store
  manifest.json    which files each source indexed, with content hashes

A knowledge source is identified by its type and resolved location (a glob),
so roles that list the same docs share one copy. Syncing is incremental:
only new or changed files are re-embedded, and deleted files are removed.

Search is scoped twice: to the knowledge sources the calling role lists, and
then to files the role may read, so the shared index can't widen access.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import Workspace
from ..resolver import Knowledge, ResolutionError, agentfw_home, resolve
from .chunking import chunk_file
from .embeddings import embeddings_spec, get_embeddings

MANIFEST_VERSION = 1
COLLECTION = "knowledge"
SKIP_DIRS = {".git", "node_modules", "target", ".venv", "build", "DerivedData", ".gradle", ".idea"}
MAX_FILE_BYTES = 2_000_000
ADD_BATCH = 256


class KnowledgeIndexError(Exception):
    """Index problems the user has to fix (e.g. built with a different embedding model)."""


@dataclass(frozen=True)
class Source:
    key: str
    type: str
    location: str
    ids: tuple[str, ...]          # knowledge source ids that point here, e.g. ("project-docs",)


@dataclass(frozen=True)
class Hit:
    path: str
    start_line: int
    heading: str
    text: str
    source_ids: tuple[str, ...]
    score: float | None = None


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    chunks: int = 0
    skipped: dict[str, str] = field(default_factory=dict)       # location -> reason
    per_source: dict[str, int] = field(default_factory=dict)    # location -> files

    def format(self) -> str:
        lines = [f"  {n:>4} files  {loc}" for loc, n in self.per_source.items()]
        lines += [f"  skipped     {loc}: {why}" for loc, why in self.skipped.items()]
        lines.append(
            f"{self.added} added, {self.updated} updated, {self.removed} removed, "
            f"{self.unchanged} unchanged; {self.chunks} chunks embedded"
        )
        return "\n".join(lines)


def source_key(kind: str, location: str) -> str:
    return hashlib.sha1(f"{kind}:{location}".encode()).hexdigest()[:16]


def sources_from(knowledge: Iterable[Knowledge]) -> list[Source]:
    """Group knowledge entries by (type, location)."""
    grouped: dict[str, tuple[Knowledge, list[str]]] = {}
    for k in knowledge:
        key = source_key(k.type, k.location)
        if key not in grouped:
            grouped[key] = (k, [])
        if k.id not in grouped[key][1]:
            grouped[key][1].append(k.id)
    return [Source(key, k.type, k.location, tuple(ids)) for key, (k, ids) in grouped.items()]


def project_sources(ws: Workspace, project_id: str) -> list[Source]:
    """Every knowledge source any enabled role in the project can search."""
    project = ws.projects[project_id].data
    tasks = [d.data for d in ws.project_handoffs(project_id).values() if d.data["type"] == "task"]
    knowledge: list[Knowledge] = []
    for entry in project["roles"]:
        if not entry.get("enabled", True):
            continue
        role = ws.roles[entry["id"]].data
        needs_task = role.get("skill_packs", {}).get("select_by") == "task.platform"
        for task in tasks if needs_task else [None]:
            try:
                knowledge += resolve(ws, project_id, entry["id"], task).knowledge
            except ResolutionError:
                continue          # validation reports these; indexing skips them
    return sources_from(knowledge)


class ProjectIndex:
    def __init__(self, project_id: str, *, embeddings: str | None = None, root: Path | None = None):
        """embeddings is a spec like 'fastembed:BAAI/bge-small-en-v1.5' (default: $AGENTFW_EMBEDDINGS)."""
        self.project_id = project_id
        self.dir = (root or agentfw_home() / "index") / project_id
        self.manifest_path = self.dir / "manifest.json"
        self.embeddings_name = embeddings or embeddings_spec()
        self._store = None
        self.manifest = self._load_manifest()

    # ---- Store -----------------------------------------------------------------

    @property
    def store(self):
        if self._store is None:
            import chromadb
            from langchain_chroma import Chroma

            self.dir.mkdir(parents=True, exist_ok=True)
            self._store = Chroma(
                collection_name=COLLECTION,
                embedding_function=get_embeddings(self.embeddings_name),
                persist_directory=str(self.dir / "chroma"),
                collection_metadata={"hnsw:space": "cosine"},
                client_settings=chromadb.config.Settings(anonymized_telemetry=False, is_persistent=True,
                                                         persist_directory=str(self.dir / "chroma")),
            )
        return self._store

    def _load_manifest(self) -> dict:
        if self.manifest_path.is_file():
            return json.loads(self.manifest_path.read_text())
        return {"version": MANIFEST_VERSION, "embeddings": self.embeddings_name, "sources": {}}

    def _save_manifest(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=2, sort_keys=True))
        tmp.replace(self.manifest_path)

    def _check_model(self) -> None:
        built_with = self.manifest.get("embeddings")
        if self.manifest["sources"] and built_with != self.embeddings_name:
            raise KnowledgeIndexError(
                f"index for {self.project_id} was built with {built_with!r}, now using {self.embeddings_name!r}; "
                f"rebuild it with: fw index {self.project_id} --rebuild"
            )

    # ---- Sync ------------------------------------------------------------------

    def sync(self, sources: Iterable[Source], *, rebuild: bool = False,
             progress: Callable[[str], None] | None = None) -> SyncReport:
        if rebuild:
            self.clear()
        self._check_model()
        report = SyncReport()
        for src in sources:
            if src.type != "repo_glob":
                report.skipped[src.location] = f"{src.type} sources aren't indexed yet"
                continue
            self._sync_source(src, report, progress)
        self.manifest["embeddings"] = self.embeddings_name
        self._save_manifest()
        return report

    def _sync_source(self, src: Source, report: SyncReport, progress) -> None:
        entry = self.manifest["sources"].setdefault(src.key, {"location": src.location, "files": {}})
        entry["ids"] = list(src.ids)
        known: dict[str, str] = entry["files"]
        current = {p: _sha256(p) for p in _files(src.location)}
        report.per_source[src.location] = len(current)

        for path in sorted(set(known) - set(current)):
            self._delete(src.key, path)
            del known[path]
            report.removed += 1
        for path, digest in current.items():
            if known.get(path) == digest:
                report.unchanged += 1
                continue
            if path in known:
                self._delete(src.key, path)
                report.updated += 1
            else:
                report.added += 1
            if progress:
                progress(path)
            report.chunks += self._add_file(src, path)
            known[path] = digest
        entry["indexed_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    def _add_file(self, src: Source, path: str) -> int:
        text = Path(path).read_text(errors="replace")
        chunks = chunk_file(path, text)
        file_id = hashlib.sha1(path.encode()).hexdigest()[:12]
        texts = [c.embedded_text for c in chunks]
        metadatas = [{"source_key": src.key, "path": c.path, "start_line": c.start_line, "heading": c.heading}
                     for c in chunks]
        ids = [f"{src.key}:{file_id}:{i}" for i in range(len(chunks))]
        for i in range(0, len(texts), ADD_BATCH):
            self.store.add_texts(texts[i:i + ADD_BATCH], metadatas=metadatas[i:i + ADD_BATCH], ids=ids[i:i + ADD_BATCH])
        return len(chunks)

    def _delete(self, key: str, path: str) -> None:
        found = self.store.get(where={"$and": [{"source_key": key}, {"path": path}]}, include=[])
        if found["ids"]:
            self.store.delete(ids=found["ids"])

    def clear(self) -> None:
        if self.manifest["sources"] or (self.dir / "chroma").exists():
            try:
                self.store.delete_collection()
            except Exception:
                pass
            self._store = None
        self.manifest = {"version": MANIFEST_VERSION, "embeddings": self.embeddings_name, "sources": {}}
        self._save_manifest()

    # ---- Search ----------------------------------------------------------------

    def indexed(self, src: Source) -> bool:
        return src.key in self.manifest["sources"]

    def search(self, query: str, sources: list[Source], *, k: int = 6, mmr: bool = True,
               readable: Callable[[str], bool] = lambda _p: True) -> list[Hit]:
        self._check_model()
        keys = [s.key for s in sources if self.indexed(s)]
        if not keys:
            return []
        ids_by_key = {s.key: s.ids for s in sources}
        where = {"source_key": keys[0]} if len(keys) == 1 else {"source_key": {"$in": keys}}
        # Ask for extra results: some may be dropped by the permission filter or as duplicates.
        want = k * 3
        if mmr:
            docs = self.store.max_marginal_relevance_search(query, k=want, fetch_k=max(40, want * 4),
                                                            lambda_mult=0.6, filter=where)
            scored = [(d, None) for d in docs]
        else:
            scored = self.store.similarity_search_with_relevance_scores(query, k=want, filter=where)

        hits, seen = [], set()
        for doc, score in scored:
            m = doc.metadata
            spot = (m["path"], m["start_line"])
            if spot in seen or not readable(m["path"]):
                continue
            seen.add(spot)
            text = doc.page_content.split("\n", 1)[1] if "\n" in doc.page_content else doc.page_content
            hits.append(Hit(m["path"], m["start_line"], m.get("heading", ""), text, ids_by_key.get(m["source_key"], ()), score))
            if len(hits) == k:
                break
        return hits

    def status(self) -> list[dict]:
        return [
            {"location": e["location"], "ids": e.get("ids", []), "files": len(e["files"]), "indexed_at": e.get("indexed_at")}
            for e in self.manifest["sources"].values()
        ]


def _files(location: str) -> list[str]:
    out = []
    for p in sorted(glob.glob(location, recursive=True)):
        parts = set(Path(p).parts)
        if os.path.isfile(p) and not (SKIP_DIRS & parts) and os.path.getsize(p) <= MAX_FILE_BYTES:
            out.append(os.path.abspath(p))
    return out


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

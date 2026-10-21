"""rag.search: semantic search over the role's knowledge sources.

Results are limited to the sources this role lists and to files it may read.
Each result carries its file path and starting line, so the agent can cite it
and open the file with fs.read.
"""
from __future__ import annotations

from ..retrieval.index import KnowledgeIndexError, ProjectIndex, sources_from
from .guard import ToolInputError

MAX_RESULT_CHARS = 1200
_indexes: dict[str, ProjectIndex] = {}


def search(ctx, query: str, sources: list[str] | None = None, k: int = 6, mmr: bool = True) -> str:
    knowledge = ctx.resolved.knowledge
    if not knowledge:
        raise ToolInputError("this role has no knowledge sources")
    if sources:
        known = {kn.id for kn in knowledge}
        unknown = [s for s in sources if s not in known]
        if unknown:
            raise ToolInputError(f"unknown source(s) {unknown}; this role can search: {sorted(known)}")
        knowledge = [kn for kn in knowledge if kn.id in sources]

    index = _index_for(ctx.resolved.project)
    srcs = sources_from(knowledge)
    missing = [s for s in srcs if not index.indexed(s)]
    try:
        hits = index.search(query, srcs, k=k, mmr=mmr, readable=ctx.resolved.permissions.can_read)
    except KnowledgeIndexError as e:
        raise ToolInputError(str(e)) from None

    note = ""
    if missing:
        note = "\nNot searched (not indexed; ask the human to run `fw index " + ctx.resolved.project + "`): " + \
            ", ".join(f"{'/'.join(s.ids)} ({s.type})" for s in missing)
    if not hits:
        return f"No results for {query!r}." + note
    lines = [f"Results for {query!r}" + (" (diversified with MMR)" if mmr else "") + ":"]
    for n, h in enumerate(hits, 1):
        heading = f" — {h.heading}" if h.heading else ""
        score = f"  score={h.score:.2f}" if h.score is not None else ""
        text = h.text if len(h.text) <= MAX_RESULT_CHARS else h.text[:MAX_RESULT_CHARS] + " …"
        lines.append(f"\n[{n}] {h.path}:{h.start_line}{heading}  (source: {', '.join(h.source_ids)}){score}\n{text}")
    return "\n".join(lines) + note


def _index_for(project_id: str) -> ProjectIndex:
    if project_id not in _indexes:
        _indexes[project_id] = ProjectIndex(project_id)
    return _indexes[project_id]


def reset_cache() -> None:
    """Forget open indexes (tests switch AGENTFW_HOME between runs)."""
    _indexes.clear()

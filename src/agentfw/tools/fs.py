"""Built-in file tools: fs.read, fs.list, fs.search, fs.write.

Every path goes through the guard. Listing and search silently skip files the
role can't read, so a search can't leak the contents of a denied file.
"""
from __future__ import annotations

import os
import re

from .guard import ToolInputError

MAX_READ_BYTES = 400_000
MAX_READ_CHARS = 20_000      # ~5k tokens; every read is re-sent on each later model call
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILE_BYTES = 1_000_000
SKIP_DIRS = {".git", "node_modules", "target", ".venv", "build", "DerivedData", ".gradle", ".idea"}


def read(ctx, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    p = ctx.guard.path(path)
    if not p.is_file():
        raise ToolInputError(f"{path} is not a file")
    if p.stat().st_size > MAX_READ_BYTES and not (start_line or end_line):
        raise ToolInputError(f"{path} is {p.stat().st_size} bytes; read it in parts with start_line/end_line")
    lines = p.read_text(errors="replace").splitlines()
    start = max(start_line or 1, 1)
    end = min(end_line or len(lines), len(lines))
    # Cut at a whole line and report the range actually returned, so the model
    # knows the rest exists and where to resume. Always return at least one line.
    out, size, last = [], 0, start - 1
    for n in range(start, end + 1):
        row = f"{n:>5}  {lines[n - 1]}"
        if out and size + len(row) + 1 > MAX_READ_CHARS:
            break
        out.append(row[:MAX_READ_CHARS])
        size += len(row) + 1
        last = n
    header = f"{p} (lines {start}-{last} of {len(lines)})"
    if last < end:
        out.append(f"... [stopped at {MAX_READ_CHARS} characters; continue with start_line={last + 1}, "
                   "or search for the part you need]")
    return header + "\n" + "\n".join(out)


def list(ctx, path: str, glob: str | None = None) -> str:   # noqa: A001 - tool name
    root = ctx.guard.path(path)
    if not root.is_dir():
        raise ToolInputError(f"{path} is not a directory")
    pattern = glob or "*"
    entries, hidden = [], 0
    for p in sorted(root.rglob(pattern) if "**" in pattern or "/" in pattern else root.glob(pattern)):
        if SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        if not _readable(ctx, p) or (p.is_dir() and not _has_readable_file(ctx, p)):
            hidden += 1
            continue
        entries.append(str(p.relative_to(root)) + ("/" if p.is_dir() else ""))
        if len(entries) >= MAX_LIST_ENTRIES:
            entries.append(f"... [stopped at {MAX_LIST_ENTRIES} entries; narrow the glob]")
            break
    note = f"\n({hidden} entries hidden: not readable by this role)" if hidden else ""
    return f"{root}:\n" + ("\n".join(entries) or "(empty)") + note


def search(ctx, pattern: str, path: str | None = None, glob: str | None = None) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolInputError(f"invalid regex: {e}") from None
    root = ctx.guard.path(path or str(ctx.guard.base_dir))
    files = [root] if root.is_file() else (root.rglob(glob) if glob else root.rglob("*"))
    matches: list[str] = []
    for f in files:
        rel_parts = f.relative_to(root).parts if f != root else ()
        if not f.is_file() or SKIP_DIRS & set(rel_parts):
            continue
        if not _readable(ctx, f) or f.stat().st_size > MAX_SEARCH_FILE_BYTES:
            continue
        data = f.read_bytes()
        if b"\x00" in data[:8192]:          # binary
            continue
        for n, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if regex.search(line):
                rel = f.relative_to(root) if f != root else f.name
                matches.append(f"{rel}:{n}: {line.strip()[:200]}")
                if len(matches) >= MAX_SEARCH_MATCHES:
                    return "\n".join(matches) + f"\n... [stopped at {MAX_SEARCH_MATCHES} matches]"
    return "\n".join(matches) if matches else "no matches"


def write(ctx, path: str, content: str) -> str:
    p = ctx.guard.path(path, write=True)
    if p.is_dir():
        raise ToolInputError(f"{path} is a directory")
    # The file itself passed the guard; creating its missing parent directories is implied.
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content)
    return f"{'Updated' if existed else 'Created'} {p} ({len(content.encode())} bytes)"


def _has_readable_file(ctx, directory, limit: int = 5000) -> bool:
    """A directory is only worth showing if the role can read something in it.
    Without this, a role limited to '*.md' could wander folders of code it can't open.
    Gives up (and shows the directory) after `limit` entries."""
    for n, f in enumerate(directory.rglob("*")):
        if n >= limit:
            return True
        if SKIP_DIRS & set(f.relative_to(directory).parts):
            continue
        if f.is_file() and _readable(ctx, f):
            return True
    return False


def _readable(ctx, p) -> bool:
    """Readable as written and after following symlinks."""
    perms = ctx.resolved.permissions
    d = os.path.isdir(p)
    return perms.can_read(p, directory=d) and perms.can_read(os.path.realpath(p), directory=d)

"""Split files into chunks that keep their place: file path, heading trail and starting line.

- Markdown: by heading (#, ##, ###), then by size. Each chunk records its heading
  trail, e.g. "Sign Up – Edge Conditions > EC-3: Email Already Registered".
- RAML: at resource boundaries (/users:, /{id}:), then by size.
- Anything else (YAML handoffs, JSON, text): by size, preferring blank lines.

The text that gets embedded starts with the file name and heading trail, so a
chunk that just says "returns 409" still carries what it belongs to.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 150
MARKDOWN_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]


@dataclass(frozen=True)
class Chunk:
    path: str
    start_line: int
    heading: str
    text: str            # the chunk as it appears in the file

    @property
    def embedded_text(self) -> str:
        label = Path(self.path).name + (f" — {self.heading}" if self.heading else "")
        return f"{label}\n{self.text}"


def chunk_file(path: str | Path, text: str) -> list[Chunk]:
    path = str(path)
    suffix = Path(path).suffix.lower()
    if suffix in (".md", ".markdown"):
        pieces = _markdown(text)
    elif suffix == ".raml":
        pieces = _split(text, ["\n/", "\n  /", "\n    /", "\ntypes:", "\n\n", "\n", " "], heading_from_resource=True)
    else:
        pieces = _split(text, ["\n\n", "\n", " "])
    return _locate(path, text, pieces)


def _markdown(text: str) -> list[tuple[str, str]]:
    sections = MarkdownHeaderTextSplitter(MARKDOWN_HEADERS, strip_headers=False).split_text(text)
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    out = []
    for section in sections:
        heading = " > ".join(section.metadata[k] for k in ("h1", "h2", "h3") if k in section.metadata)
        # The header splitter rejoins lines with "  \n"; restore the file's own line breaks.
        content = section.page_content.replace("  \n", "\n")
        out += [(heading, piece) for piece in splitter.split_text(content)]
    return out


def _split(text: str, separators: list[str], heading_from_resource: bool = False) -> list[tuple[str, str]]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, separators=separators, keep_separator="start",
    )
    out = []
    for piece in splitter.split_text(text):
        first = piece.strip().splitlines()[0] if piece.strip() else ""
        heading = first.rstrip(":") if heading_from_resource and first.startswith("/") else ""
        out.append((heading, piece))
    return out


def _locate(path: str, text: str, pieces: list[tuple[str, str]]) -> list[Chunk]:
    """Attach the starting line of each piece by finding it in the original text, scanning forward."""
    chunks, cursor = [], 0
    for heading, piece in pieces:
        body = piece.strip()
        if not body:
            continue
        # Find the chunk by its first line: splitters may change whitespace elsewhere.
        probe = body.splitlines()[0].strip()[:80]
        idx = text.find(probe, cursor)
        if idx < 0:
            idx = text.find(probe)
        line = text.count("\n", 0, idx) + 1 if idx >= 0 else 1
        if idx >= 0:
            cursor = idx + 1
        chunks.append(Chunk(path, line, heading, body))
    return chunks

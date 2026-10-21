"""Knowledge retrieval: chunking, local embeddings and the per-project Chroma index."""
from .chunking import Chunk, chunk_file
from .embeddings import HashingEmbeddings, get_embeddings
from .index import Hit, KnowledgeIndexError, ProjectIndex, Source, SyncReport, project_sources, sources_from

__all__ = [
    "Chunk", "HashingEmbeddings", "Hit", "KnowledgeIndexError", "ProjectIndex", "Source", "SyncReport",
    "chunk_file", "get_embeddings", "project_sources", "sources_from",
]

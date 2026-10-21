"""Embedding models for the knowledge index and project memory.

Default: BAAI/bge-small-en-v1.5 through fastembed (ONNX, runs locally, no
PyTorch, ~130 MB downloaded once). Free, and project docs never leave the
machine. Override with AGENTFW_EMBEDDINGS:
  fastembed:<model name>   any fastembed text model
  hashing                  bag-of-words hashing; offline and deterministic, for tests only
"""
from __future__ import annotations

import hashlib
import math
import os
import re

from langchain_core.embeddings import Embeddings

from ..resolver import agentfw_home

DEFAULT_SPEC = "fastembed:BAAI/bge-small-en-v1.5"
_cache: dict[str, Embeddings] = {}


def embeddings_spec() -> str:
    return os.environ.get("AGENTFW_EMBEDDINGS", DEFAULT_SPEC)


def get_embeddings(spec: str | None = None) -> Embeddings:
    spec = spec or embeddings_spec()
    if spec not in _cache:
        kind, _, arg = spec.partition(":")
        if kind == "fastembed":
            _cache[spec] = FastEmbedEmbeddings(arg or DEFAULT_SPEC.split(":", 1)[1])
        elif kind == "hashing":
            _cache[spec] = HashingEmbeddings()
        else:
            raise ValueError(f"unknown embeddings {spec!r} (use fastembed:<model> or hashing)")
    return _cache[spec]


class FastEmbedEmbeddings(Embeddings):
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.model_name, cache_dir=str(agentfw_home() / "models"))
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # bge models expect a query instruction prefix; query_embed adds it.
        return next(iter(self.model.query_embed([text]))).tolist()


class HashingEmbeddings(Embeddings):
    """Words hashed into a fixed-size vector. No model, no network. Good enough for tests, not for real use."""

    def __init__(self, dims: int = 512):
        self.dims = dims

    def _embed(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dims] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

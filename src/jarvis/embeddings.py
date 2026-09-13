"""Self-hosted embedding model wrapper (sentence-transformers, lazy-loaded).

The model is never loaded at MCP server startup — only on the first
embed call, mirroring ZoektLifecycle's lazy-spawn pattern. Vectors from
different models must never mix (see semantic.py's model-identity rule),
so the (model_name, revision) identity travels with every embedding.
"""

from __future__ import annotations

import os

from jarvis import runtime

DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
DEFAULT_BATCH_SIZE = 8
# bge-m3 defaults to 8192 — far beyond our 512-token chunk target and the
# tokenizer's real token count can exceed our chars//4 estimate. Capping well
# above the target but far below the model's default keeps a runaway chunk
# (or an estimate that undercounts) from blowing up encode-time memory:
# attention cost scales with batch x sequence_length^2.
MAX_SEQ_LENGTH = 1024
# Names the extra rather than a single command, because the command differs per
# install path and the previous text ("uv sync --extra semantic") only applied
# to a source checkout -- the one path a user who installed from PyPI, or via
# the Claude Code plugin, does not have.
_INSTALL_HINT = (
    "semantic search requires the 'semantic' extra — install "
    "jarvis-mcp[semantic] (uv tool install, or uvx --from), "
    "or `uv sync --extra semantic` in a source checkout"
)


def semantic_install_hint() -> str:
    if runtime.is_frozen():
        return (
            "semantic search is not included in the Homebrew binary "
            "distribution"
        )
    return _INSTALL_HINT

# Query/document instruction prefixes, by model. Matching is substring-based
# so vendor-prefixed names resolve ("intfloat/multilingual-e5-large" matches
# "e5"), and longest-pattern-first so the result is deterministic when two
# patterns both match. A future model whose name coincidentally contains a
# pattern would get the wrong prefix -- the env override exists for that case.
MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "bge-m3": ("", ""),
    "e5": ("query: ", "passage: "),
    "nomic-embed": ("search_query: ", "search_document: "),
}


class SemanticExtraMissingError(Exception):
    """The `semantic` optional dependencies are not installed."""


class EmbeddingModel:
    def __init__(self, model_name: str | None = None, revision: str | None = None,
                 batch_size: int | None = None, query_prefix: str | None = None,
                 doc_prefix: str | None = None) -> None:
        self.model_name = model_name or os.environ.get("JARVIS_EMBEDDING_MODEL", DEFAULT_MODEL)
        if revision is not None:
            self.revision = revision
        else:
            self.revision = DEFAULT_REVISION if self.model_name == DEFAULT_MODEL else "unpinned"
        self.batch_size = batch_size or int(os.environ.get("JARVIS_EMBEDDING_BATCH_SIZE",
                                                           DEFAULT_BATCH_SIZE))
        self._model = None

        env_query = os.environ.get("JARVIS_EMBEDDING_QUERY_PREFIX")
        env_doc = os.environ.get("JARVIS_EMBEDDING_DOC_PREFIX")
        matched = next(
            (MODEL_PREFIXES[p] for p in sorted(MODEL_PREFIXES, key=len, reverse=True)
             if p in self.model_name.lower()),
            None,
        )
        # Explicit args win (restoring a table's identity), then env vars, then
        # the model map. Each side resolves independently -- some models use
        # asymmetric prefixes, so setting only one env var is legal.
        base_query, base_doc = matched if matched is not None else ("", "")
        self._query_prefix = next(
            p for p in (query_prefix, env_query, base_query) if p is not None)
        self._doc_prefix = next(
            p for p in (doc_prefix, env_doc, base_doc) if p is not None)
        # Not a config problem when prefixes were passed explicitly.
        self._unlisted = (matched is None and env_query is None and env_doc is None
                          and query_prefix is None and doc_prefix is None)

    def identity(self) -> tuple[str, str]:
        return (self.model_name, self.revision)

    def prefixes(self) -> tuple[str, str]:
        """(query_prefix, doc_prefix). Env override wins, else the model map,
        else no prefix."""
        return (self._query_prefix, self._doc_prefix)

    def prefix_warning(self) -> str | None:
        """Message when the model is unlisted and no override is set, naming
        the env var to fix it. This module never prints -- semantic.py is
        imported by the MCP stdio server, and a per-query print would repeat
        on every call. Callers decide where to surface this."""
        if not self._unlisted:
            return None
        return (f"embedding model {self.model_name} is not in the known-prefix map; "
                "no query/document prefix will be applied. If this model needs one, "
                "set JARVIS_EMBEDDING_QUERY_PREFIX and JARVIS_EMBEDDING_DOC_PREFIX.")

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise SemanticExtraMissingError(semantic_install_hint()) from exc
            revision = None if self.revision == "unpinned" else self.revision
            self._model = SentenceTransformer(self.model_name, revision=revision,
                                              trust_remote_code=True)
            self._model.max_seq_length = MAX_SEQ_LENGTH
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            # normalize_embeddings: L2-normalize so cosine ranking at query
            # time is exact (standard hubness mitigation).
            for vec in model.encode(texts[i:i + self.batch_size], normalize_embeddings=True):
                vectors.append(list(vec) if not hasattr(vec, "tolist") else vec.tolist())
        return vectors

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._encode([f"{self._doc_prefix}{text}" for text in texts])

    def count_oversized(self, texts: list[str]) -> int:
        """How many of `texts` the model will silently truncate at encode
        time. Uses the real tokenizer rather than the chunker's chars//4
        estimate: that estimate is precisely what MAX_SEQ_LENGTH backstops,
        so measuring it with the same approximation would be circular.

        A separate tokenize pass is unavoidable — `encode()` tokenizes
        internally but never exposes the counts. Overhead is ~1-2% against
        the transformer forward pass. Empty input returns 0 without loading
        the model, keeping unit tests offline."""
        if not texts:
            return 0
        tokenizer = self._load().tokenizer
        oversized = 0
        for i in range(0, len(texts), self.batch_size):
            batch = [f"{self._doc_prefix}{text}" for text in texts[i:i + self.batch_size]]
            encoded = tokenizer(batch)["input_ids"]
            oversized += sum(1 for ids in encoded if len(ids) > MAX_SEQ_LENGTH)
        return oversized

    def embed_query(self, query: str) -> list[float]:
        # Deliberately NOT delegating to embed_texts: that would apply the
        # document prefix to a query.
        return self._encode([f"{self._query_prefix}{query}"])[0]


_default: EmbeddingModel | None = None


def default_model() -> EmbeddingModel:
    global _default
    if _default is None:
        _default = EmbeddingModel()
    return _default

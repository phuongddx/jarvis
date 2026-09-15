"""Unit tests for the embedding wrapper. The heavy sentence-transformers
dependency is faked via sys.modules — these tests run without the extra."""
import sys
import types

import pytest

from jarvis import embeddings
from jarvis.embeddings import EmbeddingModel, SemanticExtraMissingError


class _FakeST:
    def __init__(self, model_name, revision=None, trust_remote_code=False):
        self.model_name, self.revision = model_name, revision
        self.calls: list[list[str]] = []
        self.normalize_flags: list[bool] = []

    def encode(self, batch, normalize_embeddings=False):
        self.calls.append(list(batch))
        self.normalize_flags.append(normalize_embeddings)
        return [[float(len(t)), 1.0] for t in batch]


def _install_fake(monkeypatch):
    holder = {}

    def ctor(*args, **kwargs):
        holder["model"] = _FakeST(*args, **kwargs)
        return holder["model"]

    fake_module = types.SimpleNamespace(SentenceTransformer=ctor)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    return holder


def test_missing_extra_raises_install_hint(monkeypatch):
    from tests.conftest import BlockImportFinder
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    monkeypatch.setattr(sys, "meta_path", [BlockImportFinder("sentence_transformers"), *sys.meta_path])
    model = EmbeddingModel()
    # Asserts the extra is named, not just that some command appears: the hint
    # previously offered only `uv sync`, which is unusable for anyone who
    # installed from PyPI or through the Claude Code plugin. Escaped because
    # `match` is a regex and [semantic] would otherwise be a character class.
    with pytest.raises(
        SemanticExtraMissingError, match=r"jarvis-mcp\[semantic\]"
    ):
        model.embed_texts(["x"])


def test_lazy_load_and_batching(monkeypatch):
    holder = _install_fake(monkeypatch)
    model = EmbeddingModel(model_name="m", revision="r", batch_size=2)
    assert "model" not in holder  # nothing loaded at construction
    vectors = model.embed_texts(["a", "bb", "ccc"])
    assert vectors == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]
    assert holder["model"].calls == [["a", "bb"], ["ccc"]]  # batch_size respected
    assert holder["model"].normalize_flags == [True, True]  # L2-normalized vectors


def test_embed_query_encodes_raw_text(monkeypatch):
    # bge-m3 dropped the query-instruction-prefix requirement present in
    # earlier BGE versions, so embed_query is a direct passthrough.
    holder = _install_fake(monkeypatch)
    model = EmbeddingModel(model_name="m", revision="r")
    model.embed_query("auth")
    assert holder["model"].calls[0][0] == "auth"


def test_load_caps_max_seq_length(monkeypatch):
    # bge-m3 defaults to 8192, far beyond our chunk target; an oversized or
    # under-estimated chunk must not be free to blow encode-time memory.
    holder = _install_fake(monkeypatch)
    model = EmbeddingModel(model_name="m", revision="r")
    model.embed_texts(["x"])
    assert holder["model"].max_seq_length == embeddings.MAX_SEQ_LENGTH


def test_default_batch_size_is_conservative():
    assert EmbeddingModel().batch_size == embeddings.DEFAULT_BATCH_SIZE == 8


def test_identity_and_env_overrides(monkeypatch):
    monkeypatch.setenv("JARVIS_EMBEDDING_MODEL", "custom/model")
    monkeypatch.setenv("JARVIS_EMBEDDING_BATCH_SIZE", "7")
    model = EmbeddingModel()
    assert model.identity() == ("custom/model", "unpinned")
    assert model.batch_size == 7
    default = EmbeddingModel(model_name=embeddings.DEFAULT_MODEL)
    assert default.identity() == (embeddings.DEFAULT_MODEL, embeddings.DEFAULT_REVISION)


def test_count_oversized_returns_zero_for_empty_without_loading_model():
    """The empty guard must short-circuit before _load(), so unit tests
    never trigger a model download."""
    from jarvis.embeddings import EmbeddingModel
    model = EmbeddingModel()

    def _fail():
        raise AssertionError("model must not load for empty input")

    model._load = _fail
    assert model.count_oversized([]) == 0


def test_count_oversized_counts_texts_past_max_seq_length(monkeypatch):
    from jarvis import embeddings
    from jarvis.embeddings import EmbeddingModel

    class _FakeTokenizer:
        def __call__(self, texts):
            # one token per character keeps the lengths trivially controllable
            return {"input_ids": [list(range(len(t))) for t in texts]}

    class _FakeModel:
        tokenizer = _FakeTokenizer()

    model = EmbeddingModel(batch_size=2)
    monkeypatch.setattr(model, "_load", lambda: _FakeModel())
    short = "a" * 10
    long_text = "a" * (embeddings.MAX_SEQ_LENGTH + 1)
    assert model.count_oversized([short, long_text, long_text]) == 2


@pytest.mark.parametrize("model_name,expected", [
    ("BAAI/bge-m3", ("", "")),
    ("intfloat/multilingual-e5-large", ("query: ", "passage: ")),
    ("nomic-ai/nomic-embed-text-v1.5", ("search_query: ", "search_document: ")),
])
def test_prefixes_resolve_from_model_map(model_name, expected, monkeypatch):
    monkeypatch.delenv("JARVIS_EMBEDDING_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("JARVIS_EMBEDDING_DOC_PREFIX", raising=False)
    from jarvis.embeddings import EmbeddingModel
    assert EmbeddingModel(model_name=model_name).prefixes() == expected


def test_env_override_beats_the_map(monkeypatch):
    from jarvis.embeddings import EmbeddingModel
    monkeypatch.setenv("JARVIS_EMBEDDING_QUERY_PREFIX", "Q> ")
    monkeypatch.setenv("JARVIS_EMBEDDING_DOC_PREFIX", "D> ")
    assert EmbeddingModel(model_name="intfloat/multilingual-e5-large").prefixes() == ("Q> ", "D> ")


def test_unlisted_model_warns_and_uses_no_prefix(monkeypatch):
    monkeypatch.delenv("JARVIS_EMBEDDING_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("JARVIS_EMBEDDING_DOC_PREFIX", raising=False)
    from jarvis.embeddings import EmbeddingModel
    model = EmbeddingModel(model_name="some-vendor/unknown-model")
    assert model.prefixes() == ("", "")
    warning = model.prefix_warning()
    assert warning is not None and "JARVIS_EMBEDDING_QUERY_PREFIX" in warning


def test_listed_model_produces_no_warning(monkeypatch):
    monkeypatch.delenv("JARVIS_EMBEDDING_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("JARVIS_EMBEDDING_DOC_PREFIX", raising=False)
    from jarvis.embeddings import EmbeddingModel
    assert EmbeddingModel(model_name="BAAI/bge-m3").prefix_warning() is None


def test_query_gets_query_prefix_not_doc_prefix(monkeypatch):
    """embed_query must not inherit the document prefix by delegating to
    embed_texts -- that would be the exact bug this feature prevents."""
    monkeypatch.delenv("JARVIS_EMBEDDING_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("JARVIS_EMBEDDING_DOC_PREFIX", raising=False)
    from jarvis.embeddings import EmbeddingModel
    seen: list[str] = []

    class _FakeModel:
        def encode(self, texts, normalize_embeddings=True):
            seen.extend(texts)
            return [[0.0, 1.0] for _ in texts]

    model = EmbeddingModel(model_name="intfloat/multilingual-e5-large")
    monkeypatch.setattr(model, "_load", lambda: _FakeModel())
    model.embed_query("auth flow")
    model.embed_texts(["def f(): pass"])
    assert seen == ["query: auth flow", "passage: def f(): pass"]


def test_explicit_prefixes_win_over_env_and_map(monkeypatch):
    """Task 4 restores a table's prefixes this way -- it must beat both."""
    from jarvis.embeddings import EmbeddingModel
    monkeypatch.setenv("JARVIS_EMBEDDING_QUERY_PREFIX", "ENV> ")
    model = EmbeddingModel(model_name="intfloat/multilingual-e5-large",
                           query_prefix="TABLE> ", doc_prefix="TDOC> ")
    assert model.prefixes() == ("TABLE> ", "TDOC> ")
    assert model.prefix_warning() is None


def test_count_oversized_measures_the_prefixed_text(monkeypatch):
    """The doc prefix is part of what reaches the encoder, so it counts
    toward the sequence length the model will truncate at."""
    from jarvis import embeddings
    from jarvis.embeddings import EmbeddingModel

    class _FakeTokenizer:
        def __call__(self, texts):
            return {"input_ids": [list(range(len(t))) for t in texts]}

    class _FakeModel:
        tokenizer = _FakeTokenizer()

    model = EmbeddingModel(model_name="x", doc_prefix="P" * 20)
    monkeypatch.setattr(model, "_load", lambda: _FakeModel())
    # Body alone is under the cap; body + 20-char prefix goes over it.
    body = "a" * (embeddings.MAX_SEQ_LENGTH - 10)
    assert model.count_oversized([body]) == 1


def test_preload_loads_model_through_pinned_revision(monkeypatch):
    holder = _install_fake(monkeypatch)
    model = EmbeddingModel()
    model.preload()
    assert holder["model"] is model._model
    assert holder["model"].model_name == "BAAI/bge-m3"
    assert holder["model"].revision == embeddings.DEFAULT_REVISION

"""LanceDB vector storage + hybrid semantic search (vector + Zoekt via RRF).

A table only ever contains vectors from one model at one revision — the
model-identity rule. lancedb is imported lazily; importing this module
works without the `semantic` extra.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import config
from jarvis.chunker import (
    CONTENT_FORMAT, Chunk, _DEF_NODE_TYPES, _force_included, chunk_file, hash_file,
    iter_source_files, language_for, oversized_file_reason, skip_reason,
)
from jarvis.embeddings import EmbeddingModel, default_model
from jarvis.search import ZoektHit, ZoektUnavailableError, search_zoekt
from jarvis.symbol_search import SymbolHit, extract_tokens, search_symbols
from jarvis.syntax import ParserPool

if TYPE_CHECKING:
    from jarvis.syntax_index import SourceManifest

RRF_K = 60
VECTOR_TOP_K = 30
ZOEKT_TOP_K = 30
CONTENT_TRUNCATE = 500


# zoekt query-language field selectors and grouping characters: a query
# containing any of these is authored zoekt syntax (or a quoted/regex
# phrase) and must pass through untouched.
_ZOEKT_SYNTAX_RE = re.compile(
    r"(?:^|\s)(?:r|repo|f|file|sym|lang|l|case|content|c|branch|type|archived|fork|public|regex|meta):"
    r'|["()]'
)


def _zoekt_lexical_query(query: str) -> str:
    """The zoekt leg of semanticSearch. A natural-language query must not
    reach zoekt verbatim: the default conjunction is implicit AND, so six
    NL words typically match nothing and the lexical signal contributes
    zero on exactly the queries semanticSearch exists for. NL-shaped
    queries (multi-word, no zoekt syntax) become an OR-group of
    extract_tokens output — recall up, and zoekt's atom-count scoring
    still favors documents matching more tokens. Anything code-shaped
    (identifiers, sym:/lang: syntax, quoted phrases, grouping) passes
    through untouched.
    """
    if not query or _ZOEKT_SYNTAX_RE.search(query) or len(query.split()) <= 1:
        return query
    tokens = extract_tokens(query)
    if len(tokens) < 2:
        return query
    return "(" + " or ".join(tokens) + ")"


class NoSemanticIndexError(Exception):
    """No LanceDB table exists for the requested repo."""


@dataclass(frozen=True)
class FusedHit:
    repo: str
    file_path: str
    start_line: int
    end_line: int
    symbol_name: str | None
    content: str
    score: float
    sources: tuple[str, ...]


def _containing_chunk_key(vector_rows: list[dict], file_path: str, line: int) -> tuple | None:
    """The (file_path, start_line, end_line) key of the vector_rows entry
    whose range contains `line` in `file_path`, or None. Shared by the
    Zoekt and symbol merge rules in reciprocal_rank_fusion — both credit
    the vector chunk containing a hit's line rather than standing alone."""
    return next(
        ((r["file_path"], r["start_line"], r["end_line"]) for r in vector_rows
         if r["file_path"] == file_path and r["start_line"] <= line <= r["end_line"]),
        None,
    )


def reciprocal_rank_fusion(repo: str, vector_rows: list[dict],
                           zoekt_hits: list[ZoektHit],
                           symbol_hits: tuple[SymbolHit, ...] | list[SymbolHit] = (),
                           *, k: int = RRF_K) -> list[FusedHit]:
    entries: dict[tuple, dict] = {}

    def _add(key: tuple, rank: int, source: str, row: dict | None) -> None:
        entry = entries.setdefault(key, {"score": 0.0, "sources": [], "row": row})
        entry["score"] += 1.0 / (k + rank)
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if entry["row"] is None:
            entry["row"] = row

    for rank, row in enumerate(vector_rows, start=1):
        _add((row["file_path"], row["start_line"], row["end_line"]), rank, "vector", row)

    for rank, hit in enumerate(zoekt_hits, start=1):
        merged_key = _containing_chunk_key(vector_rows, hit.path, hit.line_number)
        if merged_key is not None:
            _add(merged_key, rank, "zoekt", None)
        else:
            _add((hit.path, hit.line_number, hit.line_number), rank, "zoekt",
                 {"file_path": hit.path, "start_line": hit.line_number,
                  "end_line": hit.line_number, "symbol_name": None, "content": hit.line_text})

    for rank, sym in enumerate(symbol_hits, start=1):
        # Same containment rule as the Zoekt merge above: credit the vector
        # chunk that contains the definition's start line, else stand alone.
        merged_key = _containing_chunk_key(vector_rows, sym.file_path, sym.start_line)
        if merged_key is not None:
            _add(merged_key, rank, "symbol", None)
        else:
            # No content: the SCIP db stores no source text, and the agent
            # has file/lines to read. symbol_name carries the dotted path.
            _add((sym.file_path, sym.start_line, sym.end_line), rank, "symbol",
                 {"file_path": sym.file_path, "start_line": sym.start_line,
                  "end_line": sym.end_line, "symbol_name": sym.dotted_path,
                  "content": ""})

    fused = [
        FusedHit(repo=repo, file_path=e["row"]["file_path"],
                 start_line=e["row"]["start_line"], end_line=e["row"]["end_line"],
                 symbol_name=e["row"]["symbol_name"],
                 content=(e["row"]["content"] or "")[:CONTENT_TRUNCATE],
                 score=e["score"], sources=tuple(e["sources"]))
        for e in entries.values()
    ]
    return sorted(fused, key=lambda h: h.score, reverse=True)


@dataclass(frozen=True)
class SkippedFile:
    file_path: str
    reason: str


@dataclass(frozen=True)
class TableIdentity:
    """Everything that must match for stored vectors to be reusable. Extends
    the model-identity rule with the prefixes actually applied at encode time
    and the format of the stored chunk text."""
    model_name: str
    model_revision: str
    query_prefix: str
    doc_prefix: str
    content_format: int


@dataclass(frozen=True)
class TokenStats:
    """Chunk-size distribution over newly-chunked content. Percentiles beat a
    bucketed histogram here: one line of CLI output that is directly
    actionable against MAX_TOKENS."""
    p50: int
    p90: int
    max: int


@dataclass(frozen=True)
class SemanticIndexReport:
    rows: int                                  # chunks written to the table
    files: int                                 # source files admitted
    skipped: tuple[SkippedFile, ...] = ()
    truncated: int | None = None               # None = could not be measured
    token_stats: TokenStats | None = None
    prefix_warning: str | None = None


class SemanticStore:
    """One LanceDB table per repo, named by slug (on disk: `<slug>.lance/`)."""

    def __init__(self, db_dir: Path) -> None:
        self._db_dir = db_dir
        self._db = None

    def _connect(self):
        if self._db is None:
            try:
                import lancedb
            except ImportError as exc:
                from jarvis.embeddings import SemanticExtraMissingError, semantic_install_hint
                raise SemanticExtraMissingError(semantic_install_hint()) from exc
            self._db_dir.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self._db_dir))
        return self._db

    def _open(self, slug: str):
        db = self._connect()
        # open_table() directly rather than checking membership via
        # table_names()/list_tables() first: both paginate (default page
        # size 10), so with more than ~10 tables in this db_dir a slug
        # sorting past the first page would look "not found" even though
        # it exists. A missing table raises ValueError — treat that as None.
        try:
            return db.open_table(slug)
        except ValueError:
            return None

    def table_identity(self, slug: str) -> TableIdentity | None:
        table = self._open(slug)
        if table is None:
            return None
        rows = table.head(1).to_pylist()
        if not rows:
            return None
        row = rows[0]
        # .get() with defaults: a table written before these columns existed
        # reads as content_format 0, which mismatches the current format and
        # forces a full rebuild instead of raising KeyError.
        return TableIdentity(
            model_name=row["model_name"],
            model_revision=row["model_revision"],
            query_prefix=row.get("query_prefix", ""),
            doc_prefix=row.get("doc_prefix", ""),
            content_format=row.get("content_format", 0),
        )

    def rows_by_path(self, slug: str) -> dict[str, list[dict]]:
        table = self._open(slug)
        if table is None:
            return {}
        grouped: dict[str, list[dict]] = {}
        for row in table.to_arrow().to_pylist():
            grouped.setdefault(row["file_path"], []).append(row)
        return grouped

    def overwrite(self, slug: str, rows: list[dict]) -> None:
        db = self._connect()
        if not rows:
            self.drop(slug)
            return
        db.create_table(slug, data=rows, mode="overwrite")

    def search(self, slug: str, vector: list[float], limit: int) -> list[dict]:
        table = self._open(slug)
        if table is None:
            return []
        # Cosine, never LanceDB's default L2 — vectors are normalized at
        # encode time, so cosine ranking is exact.
        return table.search(vector).metric("cosine").limit(limit).to_list()

    def drop(self, slug: str) -> None:
        db = self._connect()
        # ignore_missing=True: same reasoning as _open — avoid a
        # table_names()/list_tables() membership check that paginates.
        db.drop_table(slug, ignore_missing=True)


@dataclass(frozen=True)
class SemanticInput:
    """One admitted file whose vectors are not yet known -- either it
    changed since the previous index, or there was no previous index."""
    file_path: str
    source_path: Path
    file_hash: str
    language: str


@dataclass(frozen=True)
class SemanticWork:
    """Everything `finish_semantic` needs to encode and publish one repo's
    vectors, produced by `prepare_semantic`'s identity/admission pass.
    `vector_by_hash` is the one internal mutable field: `finish_semantic`
    fills it in with newly-encoded vectors as it goes. The dataclass
    itself stays frozen -- every public field is set once here and never
    reassigned."""
    slug: str
    model: EmbeddingModel
    store: SemanticStore
    identity: TableIdentity
    admitted: int
    skipped: tuple[SkippedFile, ...]
    carried: tuple[dict, ...]
    inputs: tuple[SemanticInput, ...]
    vector_by_hash: dict[str, list[float]]


def prepare_semantic(repo_path: Path, slug: str, *, root: Path | None = None,
                     model: EmbeddingModel | None = None,
                     include_prefixes: tuple[str, ...] = (),
                     manifest: SourceManifest | None = None) -> SemanticWork:
    """Identity/previous-table setup and admission/hash decisions --
    everything that does not require encoding. `model.identity()` and
    `.prefixes()` never load model weights, so this is cheap to call even
    when the model itself is unavailable. When `manifest` is given (the
    syntax-capture manifest for this same repo), a file present in it is
    read from its captured scratch bytes instead of the live worktree --
    one read shared with syntax extraction rather than two. A file absent
    from the manifest (semantic-only/untracked, or reached via
    `include_prefixes` force-include) falls back to the live path exactly
    as before."""
    model = model or default_model()
    store = SemanticStore(config.lancedb_dir(root))
    query_prefix, doc_prefix = model.prefixes()
    identity = TableIdentity(*model.identity(), query_prefix, doc_prefix, CONTENT_FORMAT)

    # Reuse rule: the previous table is only a reuse source when its full
    # identity matches -- model, prefixes, and stored-content format alike.
    previous = store.rows_by_path(slug) if store.table_identity(slug) == identity else {}
    vector_by_hash = {row["content_hash"]: row["vector"]
                      for rows in previous.values() for row in rows}

    manifest_by_path = {f.file_path: f for f in manifest.files} if manifest is not None else {}

    carried: list[dict] = []
    inputs: list[SemanticInput] = []
    skipped: list[SkippedFile] = []
    admitted = 0
    for abs_path, rel_path in iter_source_files(repo_path, include_prefixes):
        # Force-include always admits -- it must override the size cap the
        # same way it overrides the banner/long-line check below, so an
        # operator can rescue a huge generated file just as they rescue a
        # gitignored one.
        if not _force_included(rel_path, include_prefixes):
            reason = oversized_file_reason(abs_path.stat().st_size)
            if reason is not None:
                skipped.append(SkippedFile(rel_path, reason))
                continue
        captured = manifest_by_path.get(rel_path)
        if captured is not None and captured.source_path is not None:
            data = captured.source_path.read_bytes()
            file_hash = captured.file_hash or hash_file(data)
            source_path = captured.source_path
        else:
            data = abs_path.read_bytes()
            file_hash = hash_file(data)
            source_path = abs_path
        source = data.decode("utf-8", errors="replace")
        # Admission runs BEFORE the carry-forward hash check on purpose: a
        # generated file already in the table still hashes equal, so
        # filtering after the carry would keep its stale rows alive forever.
        # Skipping here makes the first post-filter reindex purge them.
        reason = skip_reason(rel_path, source, include_prefixes)
        if reason is not None:
            skipped.append(SkippedFile(rel_path, reason))
            continue
        admitted += 1
        old_rows = previous.get(rel_path)
        if old_rows and old_rows[0]["file_hash"] == file_hash:
            carried.extend(old_rows)  # unchanged file: no re-parse, no re-embed
            continue
        language = language_for(abs_path)
        inputs.append(SemanticInput(file_path=rel_path, source_path=source_path,
                                    file_hash=file_hash, language=language))

    return SemanticWork(slug=slug, model=model, store=store, identity=identity,
                        admitted=admitted, skipped=tuple(skipped),
                        carried=tuple(carried), inputs=tuple(inputs),
                        vector_by_hash=vector_by_hash)


def semantic_parse_paths(work: SemanticWork) -> frozenset[str]:
    """Which of `work.inputs` benefit from a shared parse tree -- only
    languages whose chunking actually walks AST def nodes. A fixed-window
    language re-chunks from raw text regardless of a supplied tree, so
    requesting one for it would cost a parse that chunk_file never uses."""
    return frozenset(item.file_path for item in work.inputs
                     if item.language in _DEF_NODE_TYPES)


def finish_semantic(work: SemanticWork, *, prepared_chunks: dict[str, list[Chunk]],
                    pool: ParserPool) -> SemanticIndexReport:
    """Chunk (reusing any supplied parse) and encode `work.inputs`, then
    publish the combined table. `prepared_chunks` is scoped to this one
    run: a missing entry, or one whose file_hash no longer matches this
    input (a stale/mismatched supplied chunk), falls back to chunking the
    input's own bytes rather than borrowing another generation's chunks."""
    pending: list[Chunk] = []
    for item in work.inputs:
        chunks = prepared_chunks.get(item.file_path)
        if chunks is not None and any(c.file_hash != item.file_hash for c in chunks):
            chunks = None
        if chunks is None:
            text = item.source_path.read_bytes().decode("utf-8", errors="replace")
            chunks = chunk_file(item.file_path, text, item.file_hash, item.language, pool=pool)
        pending.extend(chunks)

    unique = [c for c in {c.content_hash: c for c in pending}.values()
              if c.content_hash not in work.vector_by_hash]
    for chunk, vector in zip(unique, work.model.embed_texts([c.content for c in unique])):
        work.vector_by_hash[chunk.content_hash] = vector

    try:
        truncated = work.model.count_oversized([c.content for c in unique])
    except Exception:
        # Telemetry must never abort an otherwise-good index. index_cli
        # already wraps this whole call, so an exception here would throw
        # away a perfectly valid table over a mere counting failure.
        truncated = None

    token_counts = sorted(len(c.content) // 4 for c in pending)
    stats = TokenStats(
        p50=token_counts[len(token_counts) // 2],
        p90=token_counts[min(int(len(token_counts) * 0.9), len(token_counts) - 1)],
        max=token_counts[-1],
    ) if token_counts else None

    rows = list(work.carried) + [
        {"chunk_id": uuid.uuid4().hex, "content_hash": c.content_hash,
         "file_hash": c.file_hash, "file_path": c.file_path,
         "start_line": c.start_line, "end_line": c.end_line,
         "symbol_name": c.symbol_name or "", "language": c.language,
         "content": c.content, "vector": work.vector_by_hash[c.content_hash],
         "model_name": work.identity.model_name, "model_revision": work.identity.model_revision,
         "query_prefix": work.identity.query_prefix, "doc_prefix": work.identity.doc_prefix,
         "content_format": work.identity.content_format}
        for c in pending
    ]
    work.store.overwrite(work.slug, rows)
    return SemanticIndexReport(rows=len(rows), files=work.admitted,
                               skipped=work.skipped, truncated=truncated,
                               token_stats=stats,
                               prefix_warning=work.model.prefix_warning())


def index_semantic(repo_path: Path, slug: str, *, root: Path | None = None,
                   model: EmbeddingModel | None = None,
                   include_prefixes: tuple[str, ...] = ()) -> SemanticIndexReport:
    """Standalone entry point: prepare and finish in one call with no
    supplied parse trees. Still needed by the interactive post-install
    path, which has no syntax-capture manifest to share."""
    work = prepare_semantic(repo_path, slug, root=root, model=model,
                            include_prefixes=include_prefixes)
    return finish_semantic(work, prepared_chunks={}, pool=ParserPool())



def semantic_search(slug: str, query: str, limit: int = 10, *, root: Path | None = None,
                    zoekt_base_url: str | None = None,
                    model: EmbeddingModel | None = None,
                    scip_conn: sqlite3.Connection | None = None) -> dict:
    store = SemanticStore(config.lancedb_dir(root))
    identity = store.table_identity(slug)
    if identity is None:
        raise NoSemanticIndexError(f"no semantic index for {slug} — run jarvis reindex {slug}")

    model = model or default_model()
    warning: str | None = None
    configured = TableIdentity(*model.identity(), *model.prefixes(), CONTENT_FORMAT)
    if configured != identity:
        # Query with the model AND prefixes the table was built with, never the
        # configured ones -- applying a different prefix to the query than the
        # documents were embedded with is exactly the silent mismatch this
        # feature exists to prevent.
        warning = (f"configured embedding model {configured.model_name}@"
                   f"{configured.model_revision} (prefixes "
                   f"{configured.query_prefix!r}/{configured.doc_prefix!r}, content format "
                   f"{configured.content_format}) differs from the index's "
                   f"{identity.model_name}@{identity.model_revision} (prefixes "
                   f"{identity.query_prefix!r}/{identity.doc_prefix!r}, content format "
                   f"{identity.content_format}); queried with the index's — "
                   f"run jarvis reindex {slug} to migrate")
        model = EmbeddingModel(model_name=identity.model_name,
                               revision=identity.model_revision,
                               query_prefix=identity.query_prefix,
                               doc_prefix=identity.doc_prefix)

    vector_rows = store.search(slug, model.embed_query(query), VECTOR_TOP_K)

    zoekt_hits: list[ZoektHit] = []
    if zoekt_base_url is not None:
        try:
            zoekt_hits = search_zoekt(
                zoekt_base_url, f"r:{slug} {_zoekt_lexical_query(query)}"
            ).hits[:ZOEKT_TOP_K]
        except ZoektUnavailableError:
            pass  # hybrid degrades to vector-only; sources fields reflect it

    symbol_hits: list[SymbolHit] = []
    if scip_conn is not None:
        try:
            symbol_hits = search_symbols(scip_conn, query)
        except Exception:
            # Best-effort by contract: the symbol signal may only ever add.
            # Silent like the Zoekt degradation above; sources reflect it.
            pass

    fused = reciprocal_rank_fusion(slug, vector_rows, zoekt_hits, symbol_hits)[:limit]
    result = {
        "query": query,
        "results": [
            {"repo": h.repo, "filePath": h.file_path, "startLine": h.start_line,
             "endLine": h.end_line, "symbolName": h.symbol_name or None,
             "content": h.content, "score": h.score, "sources": list(h.sources)}
            for h in fused
        ],
        "total": len(fused),
    }
    prefix_note = model.prefix_warning()
    if prefix_note:
        warning = f"{warning}; {prefix_note}" if warning else prefix_note
    if warning:
        result["warning"] = warning
    return result

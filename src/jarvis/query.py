"""Query layer: SQL + occurrence-blob decoding against the `scip expt-convert`
v0.7.0 schema (documents/chunks/global_symbols/mentions/defn_enclosing_ranges).

Ported near-verbatim from an internal reference implementation's `query_service.py`.
Two changes from the source:

* No project/branch dimension — every method takes a bare `repo` slug
  (see config.py); the vendored `IndexConnectionCache` 3-tuple is filled in
  with pinned constants under the hood.
* `get_index_status` compares the published commit against a local
  `git rev-parse HEAD` instead of a live git-hosting API call — this is a
  personal, local-first tool with no remote service to ask. It is therefore
  synchronous, not async, and takes an optional `repo_path` (the git
  working directory to check); omitted, it can't determine staleness and
  reports the same honest "we know the commit, we haven't compared it"
  freshness the source's sync path reports before its live check runs.

``mentions.role`` note (load-bearing for every query below, preserved
verbatim from the source): it is the RAW ``SymbolRoles`` bitmask value the
v0.7.0 converter saw for a given (chunk, symbol) pair — not a normalized
0/1 boolean. Every role filter below therefore uses a bitwise AND
(``m.role & ? != 0``), never exact equality.
"""

from __future__ import annotations

import subprocess
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from jarvis.config import get_connection
from jarvis.index_reader import IndexConnectionCache, IndexMetadata, IndexNotFoundError
from jarvis.models import (
    CallHierarchyEntry,
    Coverage,
    DocumentSymbolEntry,
    Freshness,
    Location,
    Position,
    Range,
    SymbolInfo,
    TypeHierarchyEntry,
)
from jarvis.scip_decoder import (
    OccurrenceDecodeError,
    ScipOccurrence,
    SymbolRoles,
    decode_occurrences,
    decode_relationships,
    kind_name,
    scip_range_to_positions,
)
from jarvis import symbols

__all__ = [
    "CapabilityUnavailableError",
    "IndexNotFoundError",
    "OccurrenceDecodeError",
    "QueryService",
]


@dataclass(frozen=True)
class FreshnessSnapshot:
    commit: str | None
    generated_at: datetime | None
    stale: bool
    freshness: Freshness
    checked_at: datetime
    # Additive (Task 5, spec TSI-06 "Live capabilities"): the published
    # generation id, reported separately from latest-run outcome so a
    # caller can tell which immutable snapshot actually answered.
    generation: str | None = None


def _freshness_snapshot(metadata: IndexMetadata | None) -> FreshnessSnapshot:
    checked_at = datetime.now(UTC)
    if metadata is None or not metadata.commit_sha:
        return FreshnessSnapshot(
            commit=None,
            generated_at=None,
            stale=False,
            freshness=Freshness.UNKNOWN,
            checked_at=checked_at,
            generation=None,
        )

    generated_at: datetime | None = None
    if metadata.published_at:
        try:
            generated_at = datetime.fromisoformat(metadata.published_at.replace("Z", "+00:00"))
        except ValueError:
            generated_at = None

    return FreshnessSnapshot(
        commit=metadata.commit_sha,
        generated_at=generated_at,
        stale=False,
        freshness=Freshness.FRESH if generated_at is not None else Freshness.UNKNOWN,
        checked_at=checked_at,
        generation=metadata.generation,
    )


def _git_head(repo_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _occurrence_to_location(path: str, occ: ScipOccurrence) -> Location:
    start_line, start_char, end_line, end_char = scip_range_to_positions(occ.range)
    return Location(
        path=path,
        range=Range(
            start=Position(line=start_line, character=start_char),
            end=Position(line=end_line, character=end_char),
        ),
    )


def _occurrences_for_symbol(
    conn: sqlite3.Connection, symbol: str, role_bit: int | None = None
) -> list[tuple[str, ScipOccurrence]]:
    sql = (
        "SELECT DISTINCT c.id, d.relative_path, c.occurrences "
        "FROM mentions m "
        "JOIN global_symbols s ON s.id = m.symbol_id "
        "JOIN chunks c ON c.id = m.chunk_id "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE s.symbol = ?"
    )
    params: list[object] = [symbol]
    if role_bit is not None:
        sql += " AND (m.role & ?) != 0"
        params.append(role_bit)
    sql += " ORDER BY d.relative_path, c.chunk_index"

    rows = conn.execute(sql, params).fetchall()

    results: list[tuple[str, ScipOccurrence]] = []
    for _chunk_id, relative_path, blob in rows:
        for occ in decode_occurrences(blob):
            if occ.symbol == symbol:
                results.append((relative_path, occ))
    return results


def _occurrences_for_symbol_with_doc(
    conn: sqlite3.Connection, symbol: str
) -> list[tuple[int, str, ScipOccurrence]]:
    sql = (
        "SELECT DISTINCT c.document_id, d.relative_path, c.occurrences "
        "FROM mentions m "
        "JOIN global_symbols s ON s.id = m.symbol_id "
        "JOIN chunks c ON c.id = m.chunk_id "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE s.symbol = ? "
        "ORDER BY d.relative_path, c.chunk_index"
    )
    rows = conn.execute(sql, (symbol,)).fetchall()
    results: list[tuple[int, str, ScipOccurrence]] = []
    for document_id, relative_path, blob in rows:
        for occ in decode_occurrences(blob):
            if occ.symbol == symbol:
                results.append((document_id, relative_path, occ))
    return results


def _symbol_display_and_kind(conn: sqlite3.Connection, symbol: str) -> tuple[str | None, int | None]:
    row = conn.execute("SELECT display_name, kind FROM global_symbols WHERE symbol = ?", (symbol,)).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _display_and_kind(
    symbol: str, display_name: str | None, kind: int | None
) -> tuple[str | None, str | None]:
    """Resolve a symbol's display name and kind, preferring the DB columns.

    `scip expt-convert` declares `global_symbols.display_name` and `.kind`
    but never populates either (measured: 0 of 2180 rows on a real index),
    so both are always NULL today and every documentSymbols response
    carried nulls. Falling back to the symbol string fixes that.

    The fallback kind is derived from *descriptor syntax* ('#' -> TYPE,
    '().' -> METHOD), not from the indexer's semantic classification, which
    is what the integer `kind` column was meant to carry. It is therefore
    not a SCIP `SymbolKind` value. Same self-healing property as
    `relationship_data_present`: a converter that starts populating the real
    columns immediately takes precedence, with no code change here.
    """
    column_kind = kind_name(kind)
    if display_name is not None and column_kind is not None:
        return display_name, column_kind
    parsed = symbols.parse_symbol(symbol)
    if parsed is None:
        return display_name, column_kind
    return display_name or parsed.name, column_kind or str(parsed.kind)


def _symbol_info(conn: sqlite3.Connection, symbol: str) -> SymbolInfo:
    display_name, kind = _symbol_display_and_kind(conn, symbol)
    display_name, kind_label = _display_and_kind(symbol, display_name, kind)
    return SymbolInfo(symbol=symbol, displayName=display_name, kind=kind_label)


def _document_path(conn: sqlite3.Connection, document_id: int) -> str | None:
    row = conn.execute("SELECT relative_path FROM documents WHERE id = ?", (document_id,)).fetchone()
    return row[0] if row is not None else None


def _innermost_enclosing_definition(
    conn: sqlite3.Connection, document_id: int, line: int
) -> tuple[str, str | None, int | None] | None:
    row = conn.execute(
        "SELECT s.symbol, s.display_name, s.kind "
        "FROM defn_enclosing_ranges r "
        "JOIN global_symbols s ON s.id = r.symbol_id "
        "WHERE r.document_id = ? AND r.start_line <= ? AND r.end_line >= ? "
        "ORDER BY (r.end_line - r.start_line) ASC LIMIT 1",
        (document_id, line, line),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2]


def _call_hierarchy_incoming(conn: sqlite3.Connection, symbol: str) -> list[CallHierarchyEntry]:
    entries: list[CallHierarchyEntry] = []
    for document_id, relative_path, occ in _occurrences_for_symbol_with_doc(conn, symbol):
        if occ.is_definition():
            continue
        start_line, _start_char, _end_line, _end_char = scip_range_to_positions(occ.range)
        enclosing = _innermost_enclosing_definition(conn, document_id, start_line)
        if enclosing is None:
            continue  # module top-level reference — documented, not an error
        caller_symbol, display_name, kind = enclosing
        entries.append(
            CallHierarchyEntry(
                symbol=SymbolInfo(symbol=caller_symbol, displayName=display_name, kind=kind_name(kind)),
                location=_occurrence_to_location(relative_path, occ),
            )
        )
    return entries


def _call_hierarchy_outgoing(conn: sqlite3.Connection, symbol: str) -> list[CallHierarchyEntry]:
    own_ranges = conn.execute(
        "SELECT r.document_id, r.start_line, r.end_line "
        "FROM defn_enclosing_ranges r JOIN global_symbols s ON s.id = r.symbol_id "
        "WHERE s.symbol = ?",
        (symbol,),
    ).fetchall()

    entries: list[CallHierarchyEntry] = []
    seen: set[tuple[str, int, int]] = set()
    for document_id, range_start, range_end in own_ranges:
        chunk_rows = conn.execute(
            "SELECT occurrences FROM chunks WHERE document_id = ? AND start_line <= ? AND end_line >= ?",
            (document_id, range_end, range_start),
        ).fetchall()
        relative_path = _document_path(conn, document_id)
        for (blob,) in chunk_rows:
            for occ in decode_occurrences(blob):
                if occ.symbol.startswith("local ") or occ.symbol == symbol or occ.is_definition():
                    continue
                start_line, start_char, _end_line, _end_char = scip_range_to_positions(occ.range)
                if not (range_start <= start_line <= range_end):
                    continue  # chunk overlap is coarser than the exact enclosing range
                key = (occ.symbol, start_line, start_char)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    CallHierarchyEntry(
                        symbol=_symbol_info(conn, occ.symbol),
                        location=_occurrence_to_location(relative_path, occ),
                    )
                )
    return entries


def _resolve_symbol_location(conn: sqlite3.Connection, symbol: str) -> Location | None:
    row = conn.execute(
        "SELECT d.relative_path, r.start_line, r.start_char, r.end_line, r.end_char "
        "FROM defn_enclosing_ranges r "
        "JOIN global_symbols s ON s.id = r.symbol_id "
        "JOIN documents d ON d.id = r.document_id "
        "WHERE s.symbol = ? ORDER BY r.id LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is not None:
        path, start_line, start_char, end_line, end_char = row
        return Location(
            path=path,
            range=Range(
                start=Position(line=start_line, character=start_char),
                end=Position(line=end_line, character=end_char),
            ),
        )
    for _document_id, relative_path, occ in _occurrences_for_symbol_with_doc(conn, symbol):
        if occ.is_definition():
            return _occurrence_to_location(relative_path, occ)
    return None


def _type_hierarchy_supertypes(conn: sqlite3.Connection, symbol: str) -> list[TypeHierarchyEntry]:
    row = conn.execute("SELECT relationships FROM global_symbols WHERE symbol = ?", (symbol,)).fetchone()
    if row is None or row[0] is None:
        return []
    entries: list[TypeHierarchyEntry] = []
    for rel in decode_relationships(row[0]):
        if not (rel.is_implementation or rel.is_type_definition):
            continue
        location = _resolve_symbol_location(conn, rel.symbol)
        if location is None:
            continue
        entries.append(TypeHierarchyEntry(symbol=_symbol_info(conn, rel.symbol), location=location))
    return entries


def _type_hierarchy_subtypes(conn: sqlite3.Connection, symbol: str) -> list[TypeHierarchyEntry]:
    rows = conn.execute(
        "SELECT symbol, relationships FROM global_symbols WHERE relationships IS NOT NULL"
    ).fetchall()
    entries: list[TypeHierarchyEntry] = []
    for other_symbol, blob in rows:
        for rel in decode_relationships(blob):
            if rel.symbol == symbol and (rel.is_implementation or rel.is_type_definition):
                location = _resolve_symbol_location(conn, other_symbol)
                if location is None:
                    continue
                entries.append(TypeHierarchyEntry(symbol=_symbol_info(conn, other_symbol), location=location))
    return entries


def relationship_data_present(conn: sqlite3.Connection) -> bool:
    """True when any symbol carries relationship data.

    Upstream `scip expt-convert` (through v0.9.0) declares
    `global_symbols.relationships` in its schema but never writes it --
    `insertGlobalSymbols()` binds only symbol, display_name, kind,
    documentation and enclosing_symbol (verified against cmd/scip/convert.go
    at v0.9.0; scip#464). Type hierarchy is therefore unanswerable on indexes
    that converter produced, and reporting an empty result would assert that
    a type has no supertypes rather than that we cannot tell.

    Self-healing by design: the jarvis Homebrew package bundles a build
    carrying the scip#465 fix, so this flips to True on the first reindex
    with it -- no code change needed.
    """
    row = conn.execute("SELECT 1 FROM global_symbols WHERE relationships IS NOT NULL LIMIT 1").fetchone()
    return row is not None


class CapabilityUnavailableError(Exception):
    """A SCIP-only navigation tool (`findReferences`/`callHierarchy`/
    `typeHierarchy`) has no usable capability in this snapshot, or was
    asked to resolve an opaque `syntax:` identifier it structurally
    cannot use (spec TSI-05 "Unavailable precise tools return the
    established error shape plus requiredCapability/reason/recovery ...
    They must not return an empty array that implies an exhaustive
    search found no references or relationships.")."""

    def __init__(
        self, *, capability: str, message: str, reason: str, recovery: str,
        freshness: FreshnessSnapshot | None = None,
    ) -> None:
        super().__init__(message)
        self.capability = capability
        self.reason = reason
        self.recovery = recovery
        self.freshness = freshness


@dataclass(frozen=True)
class DocumentSymbolsResult:
    """documentSymbols routing result (spec TSI-05). `entries` is `None`
    exactly when `error` is set -- a per-file coverage gap is an expected,
    common outcome carried alongside `coverage`, never a raised
    exception (raising would lose the coverage detail the caller needs)."""

    entries: list[DocumentSymbolEntry] | None
    error: str | None
    coverage: Coverage | None
    freshness: FreshnessSnapshot


def _span_to_range(span) -> Range:
    return Range(
        start=Position(line=span.start_line, character=span.start_character),
        end=Position(line=span.end_line, character=span.end_character),
    )


def _syntax_location(sym, *, use_declaration: bool = False) -> Location:
    """`sym`'s identifier location by default (spec TSI-05 "Return
    identifier locations for syntax definitions"); `use_declaration=True`
    for the rarer case that wants the whole declaration span instead."""
    span = sym.declaration if use_declaration else sym.selection
    return Location(path=sym.file_path, range=_span_to_range(span), source="tree-sitter", positionEncoding="utf-8")


def _syntax_symbol_to_entry(sym) -> DocumentSymbolEntry:
    return DocumentSymbolEntry(
        symbol=sym.symbol,
        displayName=sym.name,
        kind=str(sym.kind),
        range=_span_to_range(sym.declaration),
        source="tree-sitter",
        selectionRange=_span_to_range(sym.selection),
        positionEncoding="utf-8",
        qualifiedName=sym.qualified_name,
        parentSymbol=sym.parent_symbol,
    )


def _coverage_from_facts(file_row, facts) -> Coverage:
    """Combine one file's real parse state (or its absence entirely) with
    the snapshot-wide syntax counts (spec TSI-05 "Additive result
    fields" coverage object). `file_row is None` means the path was never
    recorded in `syntax_files` at all -- untracked/uncaptured."""
    counts = facts.syntax_counts
    parsed = counts.parsed if counts else 0
    partial = counts.partial if counts else 0
    failed = counts.failed if counts else 0
    skipped = counts.skipped if counts else 0
    unsupported = counts.unsupported if counts else 0
    if file_row is None:
        return Coverage(
            state="not-indexed", reason=None, parsed=parsed, partial=partial,
            failed=failed, skipped=skipped, unsupported=unsupported,
        )
    state_map = {
        "parsed": "complete", "partial": "partial", "skipped": "partial",
        "failed": "partial", "unsupported": "unsupported",
    }
    return Coverage(
        state=state_map.get(file_row.state, "partial"), reason=file_row.reason,
        parsed=parsed, partial=partial, failed=failed, skipped=skipped, unsupported=unsupported,
    )


def _scip_document_symbols(conn: sqlite3.Connection, path: str) -> list[DocumentSymbolEntry]:
    """Exactly today's SCIP-only outline query (spec TSI-05 "For a covered
    file, SCIP remains authoritative for that operation; do not merge an
    extra syntax outline into it")."""
    outline_rows = conn.execute(
        "SELECT s.symbol, s.display_name, s.kind, "
        "r.start_line, r.start_char, r.end_line, r.end_char "
        "FROM defn_enclosing_ranges r "
        "JOIN global_symbols s ON s.id = r.symbol_id "
        "JOIN documents d ON d.id = r.document_id "
        "WHERE d.relative_path = ?",
        (path,),
    ).fetchall()

    entries: dict[str, DocumentSymbolEntry] = {}
    for symbol, display_name, kind, start_line, start_char, end_line, end_char in outline_rows:
        entry_name, entry_kind = _display_and_kind(symbol, display_name, kind)
        entries[symbol] = DocumentSymbolEntry(
            symbol=symbol,
            displayName=entry_name,
            kind=entry_kind,
            range=Range(
                start=Position(line=start_line, character=start_char),
                end=Position(line=end_line, character=end_char),
            ),
        )

    chunk_rows = conn.execute(
        "SELECT c.occurrences FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.relative_path = ?",
        (path,),
    ).fetchall()

    for (blob,) in chunk_rows:
        for occ in decode_occurrences(blob):
            if occ.symbol.startswith("local ") or occ.symbol in entries or not occ.is_definition():
                continue
            display_name, kind = _symbol_display_and_kind(conn, occ.symbol)
            entry_name, entry_kind = _display_and_kind(occ.symbol, display_name, kind)
            start_line, start_char, end_line, end_char = scip_range_to_positions(occ.range)
            entries[occ.symbol] = DocumentSymbolEntry(
                symbol=occ.symbol,
                displayName=entry_name,
                kind=entry_kind,
                range=Range(
                    start=Position(line=start_line, character=start_char),
                    end=Position(line=end_line, character=end_char),
                ),
            )

    return sorted(entries.values(), key=lambda e: (e.range.start.line, e.range.start.character))


def _genuine_scip_candidates(conn: sqlite3.Connection, query: str) -> list[symbols.Candidate]:
    """Bare/qualified-name SCIP matches that actually have a real
    definition location (spec TSI-05 "genuine SCIP definition
    candidates") -- a `global_symbols` row with no definition occurrence
    is not a promise `goToDefinition` can keep. Multiple locations for one
    matched symbol group into a single candidate (first location in
    deterministic order); never split into several candidates or used to
    pick a winner among distinct declarations."""
    matches = symbols.dotted_suffix_matches(symbols.name_map(conn), query, case_sensitive=True)
    out: list[symbols.Candidate] = []
    for match in matches:
        locations = sorted(
            (
                _occurrence_to_location(path, occ)
                for path, occ in _occurrences_for_symbol(conn, match.symbol, role_bit=SymbolRoles.DEFINITION)
                if occ.is_definition()
            ),
            key=lambda loc: (loc.path, loc.range.start.line, loc.range.start.character),
        )
        if not locations:
            continue
        out.append(symbols.Candidate(
            symbol=match.symbol, dotted_path=match.dotted_path, kind=match.kind,
            source="scip", location=locations[0],
        ))
    return out


def _syntax_candidates(conn: sqlite3.Connection, query: str) -> list[symbols.Candidate]:
    """Syntax declarations matching `query` from files without usable SCIP
    definition coverage (spec TSI-05 "syntax candidates from files
    without usable SCIP definition coverage") -- disjoint from
    `_genuine_scip_candidates` by construction, so no cross-provider
    dedup is ever needed."""
    from jarvis import syntax_index

    matches = syntax_index.find_syntax_symbols(conn, query, uncovered_only=True)
    return [
        symbols.Candidate(
            symbol=sym.symbol, dotted_path=sym.qualified_name, kind=sym.kind,
            source="tree-sitter", location=_syntax_location(sym),
        )
        for sym in matches
    ]


class QueryService:
    """Implements the 5 SCIP nav tools + getIndexStatus against index.db."""

    def __init__(self, connection_cache: IndexConnectionCache) -> None:
        self._cache = connection_cache

    def _resolved(self, conn: sqlite3.Connection, symbol: str) -> str:
        """Accept either a bare name or a full SCIP symbol. Explicit at each
        call site rather than a decorator: this codebase is consistently
        explicit, and hiding resolution would make misfires hard to trace."""
        return symbols.resolve(conn, symbol)

    def resolve_symbol(self, repo: str, symbol: str) -> str:
        """Public resolution for callers that need the canonical symbol
        string itself — `server.py` reports it as `resolvedSymbol`."""
        conn, _ = get_connection(self._cache, repo)
        return self._resolved(conn, symbol)

    def connection(self, repo: str) -> sqlite3.Connection:
        """The repo's cached read-only index connection.

        Exists for semanticSearch's symbol signal (symbol_search.py), which
        needs raw table access rather than a nav operation. Raises
        IndexNotFoundError when no index is published — callers treating
        the connection as optional catch that and pass None.
        """
        conn, _ = get_connection(self._cache, repo)
        return conn

    def get_definitions(self, repo: str, symbol: str) -> tuple[list[Location], FreshnessSnapshot]:
        conn, metadata = get_connection(self._cache, repo)
        symbol = self._resolved(conn, symbol)
        locations = [
            _occurrence_to_location(path, occ)
            for path, occ in _occurrences_for_symbol(conn, symbol, role_bit=SymbolRoles.DEFINITION)
            if occ.is_definition()
        ]
        return locations, _freshness_snapshot(metadata)

    def find_references(self, repo: str, symbol: str) -> tuple[str, list[Location], FreshnessSnapshot]:
        """ALL occurrences of `symbol`, definition sites included — no role
        filter. SCIP-only (spec TSI-05): requires real SCIP occurrence data
        and never implements lexical references or treats a syntax name
        match as a reference, even when other files in this snapshot have
        SCIP coverage."""
        from jarvis import syntax_index

        conn, metadata = get_connection(self._cache, repo)
        freshness = _freshness_snapshot(metadata)
        if symbol.startswith("syntax:"):
            raise CapabilityUnavailableError(
                capability="findReferences",
                message=(
                    f"{symbol!r} is a syntax identifier: findReferences requires real SCIP "
                    "occurrence data and never treats a syntax name match as a reference."
                ),
                reason="syntax identifiers carry no SCIP occurrence data",
                recovery=f"jarvis reindex {repo} --scip",
                freshness=freshness,
            )
        facts = syntax_index.read_snapshot_facts(conn)
        if not facts.scip_references:
            raise CapabilityUnavailableError(
                capability="findReferences",
                message=f"findReferences is unavailable for {repo}: no SCIP occurrence data in this snapshot",
                reason="no SCIP occurrence data in this snapshot",
                recovery=f"jarvis reindex {repo} --scip",
                freshness=freshness,
            )
        resolved = self._resolved(conn, symbol)
        locations = [
            _occurrence_to_location(path, occ) for path, occ in _occurrences_for_symbol(conn, resolved, role_bit=None)
        ]
        return resolved, locations, freshness

    def get_document_symbols(self, repo: str, path: str) -> DocumentSymbolsResult:
        """Route by per-file provider coverage (spec TSI-05): the file's
        SCIP outline when usable, otherwise syntax declarations from the
        same snapshot connection. A legacy snapshot with no syntax tables
        at all is served exactly as before, unconditionally."""
        from jarvis import syntax_index

        conn, metadata = get_connection(self._cache, repo)
        freshness = _freshness_snapshot(metadata)

        if not syntax_index.has_syntax_tables(conn):
            entries = _scip_document_symbols(conn, path)
            return DocumentSymbolsResult(entries=entries, error=None, coverage=None, freshness=freshness)

        file_row = syntax_index.file_provider_coverage(conn, path)
        if file_row is not None and file_row.scip_outline:
            entries = _scip_document_symbols(conn, path)
            return DocumentSymbolsResult(entries=entries, error=None, coverage=None, freshness=freshness)

        facts = syntax_index.read_snapshot_facts(conn)
        coverage = _coverage_from_facts(file_row, facts)

        if file_row is None:
            return DocumentSymbolsResult(
                entries=None, error=f"{path!r} is not tracked in this snapshot",
                coverage=coverage, freshness=freshness,
            )
        if file_row.state == "unsupported":
            return DocumentSymbolsResult(
                entries=None, error=f"{path!r} has no supported syntax grammar ({file_row.reason})",
                coverage=coverage, freshness=freshness,
            )
        if file_row.state in ("skipped", "failed"):
            return DocumentSymbolsResult(
                entries=None, error=f"{path!r} could not be parsed: {file_row.reason}",
                coverage=coverage, freshness=freshness,
            )
        syms = syntax_index.file_symbols(conn, path)
        entries = [_syntax_symbol_to_entry(sym) for sym in syms]
        return DocumentSymbolsResult(entries=entries, error=None, coverage=coverage, freshness=freshness)

    def resolve_definition(self, repo: str, symbol: str) -> tuple[str, list[Location], FreshnessSnapshot]:
        """Route goToDefinition by per-file provider coverage (spec
        TSI-05): a full SCIP identifier resolves only through SCIP, an
        opaque `syntax:` identifier only through its exact declaration,
        and a bare/qualified name through the combined candidate rule —
        genuine SCIP definition candidates plus syntax candidates from
        files without usable SCIP definition coverage. One connection
        acquisition serves resolution and location lookup together."""
        from jarvis import syntax_index

        conn, metadata = get_connection(self._cache, repo)
        freshness = _freshness_snapshot(metadata)
        has_syntax = syntax_index.has_syntax_tables(conn)

        if symbol.startswith("syntax:"):
            if not has_syntax:
                raise symbols.SymbolNotFoundError(f"no symbol named {symbol!r} in this index")
            sym = syntax_index.get_syntax_symbol(conn, symbol)
            if sym is None:
                raise symbols.SymbolNotFoundError(
                    f"syntax identifier {symbol!r} no longer resolves in this snapshot "
                    "— its declaration was changed or removed"
                )
            return symbol, [_syntax_location(sym)], freshness

        has_scip = syntax_index.has_scip_tables(conn)
        if has_scip:
            row = conn.execute("SELECT symbol FROM global_symbols WHERE symbol = ?", (symbol,)).fetchone()
            if row is not None:
                locations = [
                    _occurrence_to_location(path, occ)
                    for path, occ in _occurrences_for_symbol(conn, symbol, role_bit=SymbolRoles.DEFINITION)
                    if occ.is_definition()
                ]
                return symbol, locations, freshness

        if not has_syntax:
            # No per-file routing data at all (legacy snapshot) — exactly
            # today's bare-name behavior, SCIP only.
            resolved = self._resolved(conn, symbol)
            locations = [
                _occurrence_to_location(path, occ)
                for path, occ in _occurrences_for_symbol(conn, resolved, role_bit=SymbolRoles.DEFINITION)
                if occ.is_definition()
            ]
            return resolved, locations, freshness

        combined = (_genuine_scip_candidates(conn, symbol) if has_scip else []) + _syntax_candidates(conn, symbol)
        if not combined:
            raise symbols.SymbolNotFoundError(
                f"no symbol named {symbol!r} in this index (searched SCIP definitions and syntax declarations)"
            )
        if len(combined) == 1:
            only = combined[0]
            return only.symbol, [only.location], freshness
        ordered = sorted(combined, key=lambda c: c.dotted_path)
        raise symbols.AmbiguousSymbolError(symbol, tuple(ordered[: symbols.CANDIDATE_LIMIT]), len(ordered))


    def call_hierarchy(
        self, repo: str, symbol: str
    ) -> tuple[str, list[CallHierarchyEntry], list[CallHierarchyEntry], FreshnessSnapshot]:
        """Single-level incoming/outgoing call hierarchy. SCIP-only (spec
        TSI-05): requires real SCIP occurrence/enclosing-range data and
        never derives call edges from syntax."""
        from jarvis import syntax_index

        conn, metadata = get_connection(self._cache, repo)
        freshness = _freshness_snapshot(metadata)
        if symbol.startswith("syntax:"):
            raise CapabilityUnavailableError(
                capability="callHierarchy",
                message=(
                    f"{symbol!r} is a syntax identifier: callHierarchy requires real SCIP "
                    "occurrence/enclosing-range data, which syntax declarations never carry."
                ),
                reason="syntax identifiers carry no SCIP call-edge data",
                recovery=f"jarvis reindex {repo} --scip",
                freshness=freshness,
            )
        facts = syntax_index.read_snapshot_facts(conn)
        if not facts.scip_calls:
            raise CapabilityUnavailableError(
                capability="callHierarchy",
                message=f"callHierarchy is unavailable for {repo}: no SCIP call-edge data in this snapshot",
                reason="no SCIP occurrence/enclosing-range data in this snapshot",
                recovery=f"jarvis reindex {repo} --scip",
                freshness=freshness,
            )
        resolved = self._resolved(conn, symbol)
        incoming = _call_hierarchy_incoming(conn, resolved)
        outgoing = _call_hierarchy_outgoing(conn, resolved)
        return resolved, incoming, outgoing, freshness

    def type_hierarchy(
        self, repo: str, symbol: str
    ) -> tuple[str, list[TypeHierarchyEntry], list[TypeHierarchyEntry], FreshnessSnapshot]:
        """Single-level super/subtypes. SCIP-only (spec TSI-05): requires
        real SCIP relationship data and never derives type edges from
        syntax; preserves the distinction between absent relationship
        data and a known-empty hierarchy via `CapabilityUnavailableError`
        rather than a silently empty result."""
        from jarvis import syntax_index

        conn, metadata = get_connection(self._cache, repo)
        freshness = _freshness_snapshot(metadata)
        if symbol.startswith("syntax:"):
            raise CapabilityUnavailableError(
                capability="typeHierarchy",
                message=(
                    f"{symbol!r} is a syntax identifier: typeHierarchy requires real SCIP "
                    "relationship data, which syntax declarations never carry."
                ),
                reason="syntax identifiers carry no SCIP relationship data",
                recovery=f"jarvis reindex {repo} --scip",
                freshness=freshness,
            )
        facts = syntax_index.read_snapshot_facts(conn)
        if not facts.scip_types:
            raise CapabilityUnavailableError(
                capability="typeHierarchy",
                message=(
                    "typeHierarchy unavailable for this index: no symbol carries relationship "
                    "data. This index was built with an unpatched `scip` (upstream through "
                    "v0.9.0 never populates global_symbols.relationships — scip#464). "
                    "the jarvis Homebrew package includes a fixed build: run "
                    "`brew reinstall jarvis`, then `jarvis reindex <slug>`. "
                    "Do not read this as 'this type has no "
                    "supertypes' — it is missing data, not an empty hierarchy."
                ),
                reason="no SCIP relationship data in this snapshot",
                recovery=f"brew reinstall jarvis, then jarvis reindex {repo}",
                freshness=freshness,
            )
        resolved = self._resolved(conn, symbol)
        supertypes = _type_hierarchy_supertypes(conn, resolved)
        subtypes = _type_hierarchy_subtypes(conn, resolved)
        return resolved, supertypes, subtypes, freshness

    def get_index_status(self, repo: str, repo_path: str | None = None) -> tuple[bool, FreshnessSnapshot]:
        """`repo_path` (optional): a local git working directory to compare
        the published commit against via `git rev-parse HEAD`. Omitted, the
        published commit is reported without a staleness comparison (honest
        "we know the commit, we haven't checked" freshness, never stale=True
        without evidence)."""
        try:
            _, metadata = get_connection(self._cache, repo)
        except IndexNotFoundError:
            return False, _freshness_snapshot(None)

        snapshot = _freshness_snapshot(metadata)
        if repo_path is None or snapshot.commit is None:
            return True, snapshot

        live_head = _git_head(repo_path)
        if live_head is None:
            return True, FreshnessSnapshot(
                commit=snapshot.commit,
                generated_at=snapshot.generated_at,
                stale=False,
                freshness=Freshness.UNKNOWN,
                checked_at=snapshot.checked_at,
                generation=snapshot.generation,
            )

        stale = snapshot.commit != live_head
        return True, FreshnessSnapshot(
            commit=snapshot.commit,
            generated_at=snapshot.generated_at,
            stale=stale,
            freshness=Freshness.STALE if stale else Freshness.FRESH,
            checked_at=snapshot.checked_at,
            generation=snapshot.generation,
        )

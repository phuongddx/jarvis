"""Tests for query.py against the real-schema fixture (see
tests/fixtures/synthetic_index.py) — every method asserted against real
decoded data; nothing fabricated. Ported from an internal reference
implementation's test_query_service.py, targeting jarvis.query's
bare-`repo`-slug API."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from jarvis import config, scip_pb2
from jarvis.index_reader import IndexConnectionCache, IndexNotFoundError
from jarvis.models import Freshness, Position
from jarvis.query import CapabilityUnavailableError, QueryService
from jarvis.scip_decoder import SymbolRoles
from jarvis.symbols import AmbiguousSymbolError, SymbolNotFoundError
from jarvis.syntax import ParserPool
from jarvis.syntax_index import build_syntax_index, capture_sources, copy_syntax_tables, finalize_snapshot
from tests.fixtures.scip_encoder import encode_occurrences
from tests.fixtures.synthetic_index import (
    ANIMAL_SYMBOL,
    CLASS_SYMBOL,
    COMMIT_SHA,
    CONST_SYMBOL,
    DOC_ANIMAL,
    DOC_CONSTANTS,
    DOC_GREETER,
    METHOD_SYMBOL,
    SAY_HI_METHOD_SYMBOL,
    build_published_index,
    build_synthetic_index_db,
)

REPO = "toy-repo"


@pytest.fixture
def query_service(tmp_path: Path) -> QueryService:
    build_published_index(tmp_path, config.PROJECT, REPO, config.BRANCH)
    return QueryService(IndexConnectionCache(str(tmp_path)))


def test_get_definitions_returns_class_definition(query_service: QueryService):
    locations, freshness = query_service.get_definitions(REPO, CLASS_SYMBOL)
    assert len(locations) == 1
    assert locations[0].path == DOC_GREETER
    assert (locations[0].range.start.line, locations[0].range.start.character) == (0, 6)
    assert (locations[0].range.end.line, locations[0].range.end.character) == (0, 13)
    assert freshness.commit == COMMIT_SHA
    assert freshness.freshness == Freshness.FRESH
    assert freshness.stale is False


def test_get_definitions_returns_method_definition(query_service: QueryService):
    locations, _ = query_service.get_definitions(REPO, METHOD_SYMBOL)
    assert len(locations) == 1
    assert locations[0].path == DOC_GREETER
    assert (locations[0].range.start.line, locations[0].range.start.character) == (1, 2)


def test_get_definitions_matches_combined_bitmask_role(query_service: QueryService):
    """CONST_SYMBOL's mentions row has role=17 (Definition|Generated), not a
    bare 1 — regression-tests the bitwise-AND role filter."""
    locations, _ = query_service.get_definitions(REPO, CONST_SYMBOL)
    assert len(locations) == 1
    assert locations[0].path == DOC_GREETER
    assert (locations[0].range.start.line, locations[0].range.start.character) == (6, 6)


def test_get_definitions_unknown_symbol_raises(query_service: QueryService):
    """An unknown bare name now raises instead of returning a silently-empty
    result — the regression this feature exists to fix."""
    with pytest.raises(SymbolNotFoundError):
        query_service.get_definitions(REPO, "no-such-symbol")


def test_get_definitions_missing_index_raises_index_not_found(query_service: QueryService):
    with pytest.raises(IndexNotFoundError):
        query_service.get_definitions("does-not-exist", CLASS_SYMBOL)


def test_get_document_symbols_merges_outline_and_decoded_const_ordered(query_service: QueryService):
    result = query_service.get_document_symbols(REPO, DOC_GREETER)
    assert [e.displayName for e in result.entries] == ["Greeter", "greet", "DEFAULT_NAME", "sayHi"]


def test_find_references_returns_definition_and_usage(query_service: QueryService):
    _, locations, _ = query_service.find_references(REPO, METHOD_SYMBOL)
    paths = {loc.path for loc in locations}
    assert paths == {DOC_GREETER, DOC_CONSTANTS}


def test_call_hierarchy_returns_real_incoming_and_outgoing(query_service: QueryService):
    _, incoming, outgoing, _ = query_service.call_hierarchy(REPO, METHOD_SYMBOL)
    assert len(incoming) == 1
    assert incoming[0].symbol.symbol == SAY_HI_METHOD_SYMBOL
    assert outgoing == []


def test_call_hierarchy_unknown_symbol_raises(query_service: QueryService):
    """After wiring resolution, an unknown bare name raises instead of
    returning a silently-empty result — the whole point of this feature."""
    with pytest.raises(SymbolNotFoundError):
        query_service.call_hierarchy(REPO, "no-such-symbol")


def test_type_hierarchy_returns_real_supertype(query_service: QueryService):
    _, supertypes, subtypes, _ = query_service.type_hierarchy(REPO, CLASS_SYMBOL)
    assert len(supertypes) == 1
    assert supertypes[0].symbol.symbol == ANIMAL_SYMBOL
    assert supertypes[0].location.path == DOC_ANIMAL
    assert subtypes == []


def test_type_hierarchy_empty_for_null_relationships(query_service: QueryService):
    """METHOD_SYMBOL's relationships is NULL — the real-world v0.7.0 case
    for every symbol — must be an honest empty result, never an error."""
    _, supertypes, subtypes, _ = query_service.type_hierarchy(REPO, METHOD_SYMBOL)
    assert supertypes == []
    assert subtypes == []


def test_relationship_data_present_false_when_all_null(tmp_path):
    from jarvis.query import relationship_data_present

    db = tmp_path / "i.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE global_symbols (
            id INTEGER PRIMARY KEY, symbol TEXT, relationships BLOB
        );
        INSERT INTO global_symbols (symbol, relationships) VALUES ('a', NULL);
        INSERT INTO global_symbols (symbol, relationships) VALUES ('b', NULL);
        """
    )
    conn.commit()
    try:
        assert relationship_data_present(conn) is False
    finally:
        conn.close()


def test_relationship_data_present_true_when_any_non_null(tmp_path):
    from jarvis.query import relationship_data_present

    db = tmp_path / "i.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE global_symbols (
            id INTEGER PRIMARY KEY, symbol TEXT, relationships BLOB
        );
        INSERT INTO global_symbols (symbol, relationships) VALUES ('a', NULL);
        INSERT INTO global_symbols (symbol, relationships) VALUES ('b', X'00');
        """
    )
    conn.commit()
    try:
        assert relationship_data_present(conn) is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bare-name resolution wiring (Task 3)
# ---------------------------------------------------------------------------

def test_get_definitions_accepts_a_bare_name(query_service: QueryService):
    """The regression this whole feature exists for: a bare name used to
    return [] with no error."""
    locations, _ = query_service.get_definitions(REPO, "Greeter")
    assert len(locations) == 1
    assert locations[0].path == DOC_GREETER


def test_get_definitions_still_accepts_a_full_scip_symbol(query_service: QueryService):
    locations, _ = query_service.get_definitions(REPO, CLASS_SYMBOL)
    assert len(locations) == 1
    assert locations[0].path == DOC_GREETER


def test_find_references_accepts_a_bare_name(query_service: QueryService):
    _, by_bare, _ = query_service.find_references(REPO, "greet")
    _, by_full, _ = query_service.find_references(REPO, METHOD_SYMBOL)
    assert by_bare == by_full
    assert by_bare


def test_call_hierarchy_accepts_a_bare_name(query_service: QueryService):
    _, bare_in, bare_out, _ = query_service.call_hierarchy(REPO, "greet")
    _, full_in, full_out, _ = query_service.call_hierarchy(REPO, METHOD_SYMBOL)
    assert bare_in == full_in
    assert bare_out == full_out


def test_unknown_name_raises_instead_of_returning_empty(query_service: QueryService):
    with pytest.raises(SymbolNotFoundError):
        query_service.get_definitions(REPO, "NoSuchSymbol")


def test_resolve_symbol_returns_the_full_scip_string(query_service: QueryService):
    assert query_service.resolve_symbol(REPO, "Greeter") == CLASS_SYMBOL


def test_resolve_symbol_is_idempotent(query_service: QueryService):
    """server.py resolves, then the nav method resolves the result again.
    Rung 1 makes the second pass a no-op."""
    once = query_service.resolve_symbol(REPO, "Greeter")
    assert query_service.resolve_symbol(REPO, once) == once


def test_type_hierarchy_reports_unavailable_before_resolving(tmp_path: Path):
    """The availability check must run before resolution, so even a nonsense
    symbol gets a capability error rather than a resolution error. Uses a
    dedicated index with no relationships data (the real-world case)."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL, start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL, occurrences BLOB NOT NULL);
        CREATE TABLE global_symbols (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE,
            display_name TEXT, kind INTEGER, documentation TEXT, signature BLOB,
            enclosing_symbol TEXT, relationships BLOB);
        CREATE TABLE mentions (chunk_id INTEGER NOT NULL, symbol_id INTEGER NOT NULL,
            role INTEGER NOT NULL, PRIMARY KEY (chunk_id, symbol_id, role));
        CREATE TABLE defn_enclosing_ranges (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
            symbol_id INTEGER NOT NULL, start_line INTEGER NOT NULL, start_char INTEGER NOT NULL,
            end_line INTEGER NOT NULL, end_char INTEGER NOT NULL);
    """)
    conn.execute("INSERT INTO documents (id, relative_path) VALUES (1, 'f.ts')")
    conn.execute("INSERT INTO global_symbols (id, symbol) VALUES (1, 'local 0')")
    conn.commit()
    conn.close()

    index_dir = tmp_path / "scip" / config.PROJECT / "norel" / config.BRANCH
    index_dir.mkdir(parents=True)
    import shutil
    shutil.copy(db_path, index_dir / "index-test.db")
    (index_dir / "index-test.metadata.json").write_text('{"commit_sha":"x","published_at":"2026-01-01T00:00:00Z"}')
    (index_dir / "current").write_text("index-test.db")

    service = QueryService(IndexConnectionCache(str(tmp_path)))
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        service.type_hierarchy("norel", "NoSuchSymbol")
    assert excinfo.value.capability == "typeHierarchy"
    assert excinfo.value.recovery == "brew reinstall jarvis, then jarvis reindex norel"


def test_document_symbols_populate_display_name_and_kind(query_service: QueryService):
    """scip expt-convert leaves global_symbols.display_name and .kind NULL
    for every row, so these were always null before the parser fallback."""
    result = query_service.get_document_symbols(REPO, DOC_GREETER)
    by_symbol = {e.symbol: e for e in result.entries}
    greeter = by_symbol[CLASS_SYMBOL]
    assert greeter.displayName == "Greeter"
    assert greeter.kind == "TYPE"
    method = by_symbol[METHOD_SYMBOL]
    assert method.displayName == "greet"
    assert method.kind == "METHOD"


def test_display_name_prefers_a_populated_column_over_the_parser():
    """Self-healing: if a future converter starts populating the real
    columns, they win over the syntax-derived fallback."""
    from jarvis.query import _display_and_kind

    display_name, kind = _display_and_kind(CLASS_SYMBOL, "FromColumn", 5)
    assert display_name == "FromColumn"
    assert kind != "TYPE"  # kind_name(5) came from the column, not the parser


def test_display_and_kind_falls_back_for_unparseable_symbols():
    from jarvis.query import _display_and_kind

    assert _display_and_kind("local 0", None, None) == (None, None)


def test_query_service_exposes_repo_connection(query_service: QueryService):
    """connection() hands semanticSearch the same cached read-only conn the
    nav tools use; symbol data must be readable through it."""
    conn = query_service.connection(REPO)
    assert conn.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0] > 0


def test_query_service_connection_raises_for_unpublished_repo(query_service: QueryService):
    from jarvis.index_reader import IndexNotFoundError

    with pytest.raises(IndexNotFoundError):
        query_service.connection("no-such-repo")


# ---------------------------------------------------------------------------
# Per-file provider routing (Task 5, spec TSI-05)
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


MULTI_SYMBOL = "scip-typescript npm @toy/pkg 0.0.1 src/`multi.ts`/Multi#"


def _add_two_location_scip_symbol(conn: sqlite3.Connection) -> None:
    """Real-schema SCIP data the base fixture lacks: one symbol with TWO
    genuine definition occurrences (chunk-encoded blobs plus matching
    mentions rows, exactly what `convert.go` would emit for two
    definitions of one symbol), so the combined candidate rule's
    "multiple locations of one genuine SCIP symbol group as one
    candidate" clause is exercised against the public API."""
    doc_id = conn.execute("SELECT MAX(id) FROM documents").fetchone()[0] + 1
    symbol_id = conn.execute("SELECT MAX(id) FROM global_symbols").fetchone()[0] + 1
    conn.execute("INSERT INTO documents (id, relative_path) VALUES (?, ?)", (doc_id, "toy/multi.ts"))
    conn.execute(
        "INSERT INTO global_symbols (id, symbol, display_name) VALUES (?, ?, 'Multi')",
        (symbol_id, MULTI_SYMBOL),
    )
    for chunk_index, (start_line, start_char, end_char) in enumerate(((0, 6, 11), (4, 6, 11))):
        chunk_id = conn.execute("SELECT MAX(id) FROM chunks").fetchone()[0] + 1
        occurrence = scip_pb2.Occurrence(
            range=[start_line, start_char, end_char],
            symbol=MULTI_SYMBOL,
            symbol_roles=SymbolRoles.DEFINITION,
        )
        conn.execute(
            "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, doc_id, chunk_index, start_line, start_line, encode_occurrences([occurrence])),
        )
        conn.execute(
            "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?, ?, ?)",
            (chunk_id, symbol_id, int(SymbolRoles.DEFINITION)),
        )
    conn.commit()


def build_combined_snapshot(tmp_path: Path, project: str, repo: str, branch: str) -> Path:
    """One immutable snapshot combining the real SCIP fixture
    (`build_synthetic_index_db`) with genuine syntax rows for a real git
    repo's extra files (Task 3/4's capture/build/copy/finalize pipeline).
    `toy/greeter.ts` lands under real SCIP outline+definition coverage
    (it shares its path and `Greeter` class name with the fixture's
    `CLASS_SYMBOL`); every other file is syntax-only, exercising every
    per-file coverage state through the real pipeline, never mocked."""
    repo_dir = tmp_path / "combined-repo"
    _init_repo(repo_dir)
    toy = repo_dir / "toy"
    toy.mkdir()
    (toy / "greeter.ts").write_text("class Greeter {}\n")
    (toy / "util.py").write_text("def helper():\n    return 1\n")
    (toy / "empty.py").write_text("# nothing here\n")
    (toy / "notes.txt").write_text("hello\n")
    (toy / "bad.py").write_bytes(b"\xff\xfe\x01\x02")
    (toy / "huge.py").write_text("# pad\n" * 200_000)
    (toy / "partial.py").write_text("def good():\n    pass\n\n\ndef bad(:\n    pass\n")
    (toy / "dup1.py").write_text("def dup():\n    pass\n")
    (toy / "dup2.py").write_text("def dup():\n    pass\n")
    # A syntax declaration colliding with a genuine SCIP name (`greet`)
    # from an uncovered file — the mixed-provider candidate case.
    (toy / "extra.py").write_text("def greet():\n    pass\n")
    # Two declarations deliberately in non-alphabetical source order.
    (toy / "order.py").write_text("def zebra():\n    pass\n\n\ndef aardvark():\n    pass\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "toy"], check=True)

    manifest = capture_sources(repo_dir, tmp_path / "combined-scratch")
    syntax_db = tmp_path / "combined-syntax.db"
    build_syntax_index(syntax_db, manifest, pool=ParserPool())

    index_dir = tmp_path / "scip" / project / repo / branch
    index_dir.mkdir(parents=True, exist_ok=True)
    db_filename = f"index-{COMMIT_SHA}.db"
    db_path = index_dir / db_filename
    build_synthetic_index_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        _add_two_location_scip_symbol(conn)
        copy_syntax_tables(syntax_db, conn)
        finalize_snapshot(
            conn, generation="gen-combined", commit_sha=COMMIT_SHA, published_at="2026-07-08T12:00:00Z",
            source_hash=manifest.source_hash, scip_state="available",
        )
    finally:
        conn.close()

    metadata = {
        "project": project, "repo": repo, "branch": branch,
        "commit_sha": COMMIT_SHA, "published_at": "2026-07-08T12:00:00Z",
        "generation": "gen-combined", "source_hash": manifest.source_hash,
    }
    (index_dir / f"index-{COMMIT_SHA}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (index_dir / "current").write_text(db_filename, encoding="utf-8")
    return index_dir


def build_syntax_only_snapshot(tmp_path: Path, project: str, repo: str, branch: str) -> Path:
    """A snapshot with syntax tables but zero real SCIP tables at all —
    every SCIP capability fact is genuinely absent, not merely empty."""
    repo_dir = tmp_path / "syntax-only-repo"
    _init_repo(repo_dir)
    (repo_dir / "solo.py").write_text("def solo():\n    pass\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "solo"], check=True)

    manifest = capture_sources(repo_dir, tmp_path / "syntax-only-scratch")
    index_dir = tmp_path / "scip" / project / repo / branch
    index_dir.mkdir(parents=True, exist_ok=True)
    db_filename = "index-solo.db"
    db_path = index_dir / db_filename
    build_syntax_index(db_path, manifest, pool=ParserPool())

    conn = sqlite3.connect(db_path)
    try:
        finalize_snapshot(
            conn, generation="gen-solo", commit_sha="solo123", published_at="2026-07-08T12:00:00Z",
            source_hash=manifest.source_hash, scip_state="disabled",
        )
    finally:
        conn.close()

    metadata = {
        "project": project, "repo": repo, "branch": branch,
        "commit_sha": "solo123", "published_at": "2026-07-08T12:00:00Z",
        "generation": "gen-solo", "source_hash": manifest.source_hash,
    }
    (index_dir / "index-solo.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (index_dir / "current").write_text(db_filename, encoding="utf-8")
    return index_dir


@pytest.fixture
def combined_service(tmp_path: Path) -> QueryService:
    build_combined_snapshot(tmp_path, config.PROJECT, REPO, config.BRANCH)
    return QueryService(IndexConnectionCache(str(tmp_path)))


@pytest.fixture
def syntax_only_service(tmp_path: Path) -> QueryService:
    build_syntax_only_snapshot(tmp_path, config.PROJECT, "solo-repo", config.BRANCH)
    return QueryService(IndexConnectionCache(str(tmp_path)))


def test_document_symbols_scip_covered_file_uses_scip_outline_unchanged(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, DOC_GREETER)
    assert result.error is None
    assert result.coverage is None
    assert [e.displayName for e in result.entries] == ["Greeter", "greet", "DEFAULT_NAME", "sayHi"]


def test_document_symbols_syntax_only_file_returns_source_order_declarations(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/util.py")
    assert result.error is None
    assert result.coverage.state == "complete"
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.displayName == "helper"
    assert entry.source == "tree-sitter"
    assert entry.positionEncoding == "utf-8"
    assert entry.qualifiedName == "helper"
    assert entry.parentSymbol is None
    assert entry.selectionRange is not None


def test_document_symbols_zero_declaration_parsed_file_is_complete(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/empty.py")
    assert result.error is None
    assert result.entries == []
    assert result.coverage.state == "complete"


def test_document_symbols_unsupported_extension_reports_coverage(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/notes.txt")
    assert result.entries is None
    assert result.error is not None
    assert result.coverage.state == "unsupported"


def test_document_symbols_untracked_path_is_not_indexed(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/does-not-exist.py")
    assert result.entries is None
    assert result.error is not None
    assert result.coverage.state == "not-indexed"


def test_document_symbols_undecodable_file_is_partial_with_reason(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/bad.py")
    assert result.entries is None
    assert result.error is not None
    assert result.coverage.state == "partial"
    assert result.coverage.reason is not None


def test_document_symbols_oversized_file_is_partial_with_size_reason(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/huge.py")
    assert result.entries is None
    assert result.error is not None
    assert result.coverage.state == "partial"
    assert result.coverage.reason == "file exceeds 1 MiB syntax limit"


def test_document_symbols_partially_parsed_file_keeps_surviving_declarations(combined_service: QueryService):
    result = combined_service.get_document_symbols(REPO, "toy/partial.py")
    assert result.error is None
    assert {e.displayName for e in result.entries} == {"good", "bad"}
    assert result.coverage.state == "partial"
    assert result.coverage.reason == "grammar produced error or missing nodes"


def test_resolve_definition_full_scip_identifier_uses_scip_only(combined_service: QueryService):
    resolved, locations, _ = combined_service.resolve_definition(REPO, CLASS_SYMBOL)
    assert resolved == CLASS_SYMBOL
    assert len(locations) == 1
    assert locations[0].path == DOC_GREETER
    assert locations[0].source == "scip"


def test_resolve_definition_syntax_identifier_resolves_to_exact_declaration(combined_service: QueryService):
    outline = combined_service.get_document_symbols(REPO, "toy/util.py")
    syntax_id = outline.entries[0].symbol
    resolved, locations, _ = combined_service.resolve_definition(REPO, syntax_id)
    assert resolved == syntax_id
    assert len(locations) == 1
    assert locations[0].path == "toy/util.py"
    assert locations[0].source == "tree-sitter"
    assert locations[0].positionEncoding == "utf-8"


def test_resolve_definition_stale_syntax_identifier_is_not_found_never_redirected(combined_service: QueryService):
    with pytest.raises(SymbolNotFoundError):
        combined_service.resolve_definition(REPO, "syntax:" + "0" * 64)


def test_resolve_definition_bare_name_finds_the_sole_syntax_candidate(combined_service: QueryService):
    resolved, locations, _ = combined_service.resolve_definition(REPO, "helper")
    assert resolved.startswith("syntax:")
    assert len(locations) == 1
    assert locations[0].source == "tree-sitter"
    assert locations[0].path == "toy/util.py"


def test_resolve_definition_bare_name_prefers_the_scip_covered_file_over_syntax(combined_service: QueryService):
    resolved, locations, _ = combined_service.resolve_definition(REPO, "Greeter")
    assert resolved == CLASS_SYMBOL
    assert locations[0].source == "scip"
    assert locations[0].path == DOC_GREETER


def test_resolve_definition_bare_name_zero_candidates_raises_not_found(combined_service: QueryService):
    with pytest.raises(SymbolNotFoundError):
        combined_service.resolve_definition(REPO, "NoSuchNameAnywhere")


def test_resolve_definition_bare_name_multiple_syntax_candidates_raises_ambiguous_with_locations(
    combined_service: QueryService,
):
    with pytest.raises(AmbiguousSymbolError) as excinfo:
        combined_service.resolve_definition(REPO, "dup")
    candidates = excinfo.value.candidates
    assert len(candidates) == 2
    paths = {c.location.path for c in candidates}
    assert paths == {"toy/dup1.py", "toy/dup2.py"}
    assert all(c.source == "tree-sitter" for c in candidates)



def test_resolve_definition_groups_multiple_scip_locations_into_one_candidate(
    combined_service: QueryService,
):
    """One genuine SCIP symbol defined at two locations (toy/multi.ts
    lines 0 and 4) must resolve as ONE candidate — never one candidate
    per location, which would surface as a false ambiguity."""
    resolved, locations, _ = combined_service.resolve_definition(REPO, "Multi")
    assert resolved == MULTI_SYMBOL
    assert len(locations) == 1
    assert locations[0].source == "scip"
    assert locations[0].path == "toy/multi.ts"
    assert (locations[0].range.start.line, locations[0].range.start.character) == (0, 6)


def test_resolve_definition_bare_name_mixes_scip_and_syntax_candidates_without_winner_picking(
    combined_service: QueryService,
):
    """An uncovered-file syntax `greet` collides with the genuine SCIP
    `greet` method: the combined rule must yield BOTH candidates as a
    structured ambiguity — coverage-based exclusion (what this router
    implements) and prohibited name-based winner-picking (SCIP always
    wins) are indistinguishable to every single-provider test."""
    with pytest.raises(AmbiguousSymbolError) as excinfo:
        combined_service.resolve_definition(REPO, "greet")
    candidates = excinfo.value.candidates
    assert len(candidates) == 2
    assert {c.source for c in candidates} == {"scip", "tree-sitter"}
    scip_candidate = next(c for c in candidates if c.source == "scip")
    assert scip_candidate.symbol == METHOD_SYMBOL
    assert scip_candidate.location is not None and scip_candidate.location.path == DOC_GREETER
    syntax_candidate = next(c for c in candidates if c.source == "tree-sitter")
    assert syntax_candidate.location is not None and syntax_candidate.location.path == "toy/extra.py"


def test_document_symbols_syntax_file_returns_source_order_with_zero_based_lines(
    combined_service: QueryService,
):
    """order.py declares zebra before aardvark on purpose: the outline
    must be in source order (never alphabetical), on zero-based lines —
    a one-based regression would report lines 1 and 5."""
    result = combined_service.get_document_symbols(REPO, "toy/order.py")
    assert [e.displayName for e in result.entries] == ["zebra", "aardvark"]
    zebra, aardvark = result.entries
    assert zebra.source == "tree-sitter" and aardvark.source == "tree-sitter"
    assert zebra.selectionRange.start == Position(line=0, character=4)
    assert zebra.selectionRange.end.character == 9
    assert aardvark.selectionRange.start == Position(line=4, character=4)
    assert zebra.range.start.line == 0
    assert aardvark.range.start.line == 4


def test_find_references_raises_capability_error_when_scip_absent(syntax_only_service: QueryService):
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        syntax_only_service.find_references("solo-repo", "solo")
    assert excinfo.value.capability == "findReferences"


def test_call_hierarchy_raises_capability_error_when_scip_absent(syntax_only_service: QueryService):
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        syntax_only_service.call_hierarchy("solo-repo", "solo")
    assert excinfo.value.capability == "callHierarchy"


def test_type_hierarchy_raises_capability_error_when_scip_absent(syntax_only_service: QueryService):
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        syntax_only_service.type_hierarchy("solo-repo", "solo")
    assert excinfo.value.capability == "typeHierarchy"


def test_find_references_rejects_a_syntax_identifier_even_with_scip_coverage_elsewhere(
    combined_service: QueryService,
):
    outline = combined_service.get_document_symbols(REPO, "toy/util.py")
    syntax_id = outline.entries[0].symbol
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        combined_service.find_references(REPO, syntax_id)
    assert excinfo.value.capability == "findReferences"


def test_call_hierarchy_rejects_a_syntax_identifier_even_with_scip_coverage_elsewhere(
    combined_service: QueryService,
):
    outline = combined_service.get_document_symbols(REPO, "toy/util.py")
    syntax_id = outline.entries[0].symbol
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        combined_service.call_hierarchy(REPO, syntax_id)
    assert excinfo.value.capability == "callHierarchy"


def test_type_hierarchy_rejects_a_syntax_identifier_even_with_scip_coverage_elsewhere(
    combined_service: QueryService,
):
    outline = combined_service.get_document_symbols(REPO, "toy/util.py")
    syntax_id = outline.entries[0].symbol
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        combined_service.type_hierarchy(REPO, syntax_id)
    assert excinfo.value.capability == "typeHierarchy"

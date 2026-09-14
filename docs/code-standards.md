# jarvis: Code Standards & Conventions

This document describes the patterns and conventions actually observed in the jarvis codebase, derived from the implementation and architectural decisions made across all 4 phases.

## Architecture & Design Patterns

### 1. Dataclass-First Type System

**Pattern:** All result types (nav/search results) are immutable frozen dataclasses, not Pydantic models.

**Why:** 
- jarvis has no HTTP API boundary that would benefit from Pydantic validation
- MCP tool functions return dicts built directly from dataclasses via `dataclasses.asdict()`
- Minimal validation overhead; focus on correctness at the source (database query builder)

**Examples:**
```python
@dataclass(frozen=True)
class Position:
    line: int
    character: int

@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    displayName: str | None = None
    kind: str | None = None
```

**Convention:** Use `| None` union syntax (Python 3.10+) instead of `Optional[T]`. All result dataclasses are frozen.

---

### 2. Isolation Seams: Protobuf & zstandard

**Pattern:** `scip_decoder.py` is the single module importing `scip_pb2` and `zstandard`.

**Why:**
- Future SCIP proto version bumps are localized to one file
- Tests can mock zstandard decompression without touching other modules
- Easier to track upstream API drift (scip.proto changes in sourcegraph/scip)

**Example isolation:**
```python
# scip_decoder.py (only imports scip_pb2)
import zstandard
from jarvis.scip_pb2 import Document

# query.py, search.py (NO scip_pb2 imports; delegate to scip_decoder)
from jarvis.scip_decoder import scip_range_to_positions
```

**Convention:** Isolation seams are explicitly commented in module docstrings.

---

### 3. Atomic Pointer-Swap Publishing

**Pattern:** Index publishing writes a new versioned `.db`, waits for graph/Zoekt completion, then atomically swaps the `current` pointer file via `os.replace()`.

**Why:**
- Queries reading the old index never see partial state
- A failure anywhere in the pipeline leaves the previously published index live
- Zero query downtime across reindex

**Implementation (index_cli.py):**
```python
# Write new index to temp file
new_db_path = index_dir / f"index-{commit_sha}.db"

# Run all expensive operations
populate_graph_for_repo(repo_slug, symbols)
zoekt_index(new_db_path)

# Only once all succeed, atomically swap
current_pointer = index_dir / "current"
os.replace(new_db_path, current_pointer)
```

**Convention:** Pointer files are small text files (1 line: the commit SHA or db name). Never write a pointer until *all* expensive operations complete.

---

### 4. Rebuild-Not-Accumulate Graph

**Pattern:** Each reindex clears that repo's outgoing package dependencies before recomputing them.

**Why:**
- Removed dependencies are properly retracted (not stale in the database)
- `blastRadius` always reflects each repo's *last* index run
- No accumulation bugs from partial re-runs or tool failures

**Implementation (graph.py):**
```python
def populate_graph_for_repo(repo_slug: str, symbols: list[str]) -> None:
    # DELETE old edges for this repo
    db.execute("DELETE FROM edges WHERE source_repo = ?", (repo_slug,))
    
    # INSERT new edges
    for pkg_name in extract_package_names(symbols):
        db.execute("INSERT INTO edges ...", (repo_slug, pkg_name, ...))
```

**Convention:** Always clear before rebuild in batch operations. Document the rebuild expectation in docstrings.

---

### 5. Direct sqlite3 Usage (No ORM)

**Pattern:** Raw SQL executed via stdlib `sqlite3` module; no SQLAlchemy, Tortoise, or GRDB.

**Why:**
- jarvis's schema is small (5-6 tables max): `repos`, `packages`, `edges` (registry.db); `documents`, `chunks`, `global_symbols`, `mentions`, `defn_enclosing_ranges` (index-*.db)
- SCIP schema is output of `scip expt-convert` — not a designed API, treated as moving target
- Zero external database dependency; easier to embed in single-user tool

**Exceptions:**
- `index_reader.py` (vendored from SCIP source) does use sqlite3 with some optimization flags (`mode=ro&immutable=1`)

**Convention:** Parameterized queries always; no string interpolation for user input (e.g., repo slugs).

---

### 6. Broad Exception Handling in MCP Server

**Pattern:** `server.py` catches all exceptions at the MCP tool boundary and returns uniform error payload: `{"error": "...message..."}`.

**Why:**
- Keeps the stdio server alive even on query bugs
- Consistent error format for MCP clients
- Separates internal exception details from user-facing messages

**Implementation (server.py):**
```python
@mcp.tool()
def goToDefinition(repo: str, path: str, line: int, character: int):
    try:
        result = _service().go_to_definition(repo, path, line, character)
        return {"location": asdict(result)} if result else {"location": None}
    except Exception as e:
        return {"error": str(e)}
```

**Convention:** No bare `except` without rationale. Broad catches are documented with "by design" comments.

---

### 7. Environment Variable Overrides

**Pattern:** Configuration via env vars with sensible defaults.

**Current overrides:**
- `JARVIS_DATA_DIR` → override `~/.jarvis` (default)
- Future: `JARVIS_ZOEKT_BIN` → override zoekt-webserver binary path

**Convention:** Prefix all env vars with `JARVIS_`. Use `.expanduser()` for path variables. Document in README and config.py docstring.

---

### 8. Base Grammar Dependencies and Optional Extras

**Pattern:** The Tree-sitter runtime and every curated grammar package are
**base dependencies** (`pyproject.toml` `[project.dependencies]`, spec TSI-02);
heavier, truly optional capabilities stay gated behind
`[project.optional-dependencies]`. Two extras exist today:
- `watch = ["watchdog>=4.0"]` — needed for `jarvis watch` in a source checkout; the standalone build bundles it
- `semantic = ["lancedb>=0.20", "sentence-transformers>=3.0"]` — needed for source-build `semanticSearch`; the Homebrew distribution deliberately excludes it

(The grammar packages moved from the old `tree-sitter-language-pack`-based
semantic extra to base dependencies: the syntax baseline must parse offline
from prebuilt abi3 wheels with no extra installed and no download at index
time.)

**Why:**
- A base install has no ML/native-binding dependencies beyond the grammar wheels
- Every import of an extra's packages is deferred inside the function that needs it, never at
  module top-level — so a base install can still import every module in `src/jarvis/`
  without the extra installed
- Missing an extra fails narrowly and legibly at the point of use (e.g. `semantic.py` raises
  `SemanticExtraMissingError`, which `index_cli.py`'s semantic stage catches and skips with a
  one-line hint); by contrast, a missing *base* grammar raises `SyntaxDependencyError`
  immediately and loudly, because a broken base install is a packaging bug, not an optional
  capability

**Convention:** Install extras with `uv sync --extra <name>`. New optional capabilities should follow
this same shape: add the extra, defer its imports, and fail with a specific, catchable exception
when it's missing.

---

### 9. Release-Version Guard Pattern

**Pattern:** `pyproject.toml [project].version` is the sole local release-version declaration. A dedicated script (`scripts/check_versions.py`) verifies that declaration is readable; it runs via an automated test (`tests/test_check_versions.py`) and CI. A release additionally runs `uv lock` so the lockfile's self-referential project entry follows the bump.

**Why:**
- Duplicating a version across descriptors invites partial bumps and an
  unusable release
- Homebrew archives, their checksums, and the generated formula are all keyed
  by one version, so the source of that value must be unambiguous
- Test gates all CI pipelines — an unreadable version fails the release

**Implementation:** `check_versions.py` parses `pyproject.toml` and returns the
single declared release version. It does not inspect plugin manifests in
`jarvis-intelligence/jarvis-index`, which version independently. The test wraps
the script and runs on every CI push/PR.

**Convention:** Do not add another local release-version declaration. Keep
`uv.lock` generated by `uv lock`, never hand-edited.

---

### 11. Single-Tenant Hardcoding

**Pattern:** `PROJECT = "_"` and `BRANCH = "_"` are pinned constants in `config.py`; the vendored `IndexConnectionCache` keys on `(project, repo, branch)`, but jarvis uses only `repo`.

**Why:**
- Single user, one repo per slug
- Reuse vendored cache code without modification
- Disk path `scip/_/<slug>/_/` is an artifact of the cache's path shape

**Convention:** These constants are intentionally hardcoded and not configurable. Document clearly in config.py docstring if ever tempted to make them dynamic.

---

### 12. Model-Identity-Locked Vector Store

**Pattern:** A `SemanticStore` LanceDB table (`semantic.py`) only ever holds vectors from one
`TableIdentity` at a time — model name, model revision, query prefix, doc prefix, and
`CONTENT_FORMAT` (chunker.py's version of the stored chunk-text shape) — recorded in the table
itself (`table_identity()`), not inferred from current config.

**Why:**
- Embedding spaces from different models (or model revisions) are not comparable — mixing them
  silently would rank results by meaningless distances
- A prefix change or a `CONTENT_FORMAT` bump changes what was actually embedded just as much as a
  model change does — folding both into the identity means a table is only reused when the whole
  identity matches, so a file whose bytes never changed can't carry header-less/wrong-prefix rows
  forward forever (carry-forward keys on `file_hash`, not `content_hash`)
- `index_semantic()` always fully re-embeds every chunk when any part of the identity changes; the
  old table's vectors are never reused
- `semantic_search()` embeds the query using the table's *recorded* identity (model, revision, and
  prefixes — never the currently configured ones), and includes a `"warning"` in results if that
  differs from the currently configured identity — nudging a reindex instead of silently returning
  wrong-space results

**Convention:** Never compare or merge vectors across table identities. Any change to the default
embedding model, its prefixes, or `CONTENT_FORMAT` is a data-migration event (full reindex), not a
config tweak.

### 13. Curated Grammar Provider

**Pattern:** `syntax.py` owns the one finite map from internal language name to
grammar source — `FACTORIES: dict[str, tuple[str, str, str]]`, mapping 17
language names to 17 pinned `(distribution, module, factory)` triples over 16
distributions (TypeScript and TSX are two factories from one distribution; PHP
selects the PHP-with-tags factory). Repository-supplied grammar code is never
instantiated.

**Why:**
- A finite, reviewed map is auditable: no grab-bag provider can silently
  resolve a language to an unexpected grammar or fetch one at runtime
- Grammar identity is pinned per distribution, so stored rows can be keyed on
  a `grammar_identity()` digest (extractor version + runtime version + grammar
  versions) and re-extracted exactly when the extractor changes

**Conventions:**
- Lazy imports: `tree_sitter` types are `TYPE_CHECKING`-only; grammar modules
  load inside `ParserPool` on first use per worker, never at module import
- Frozen dataclasses for all extracted results (`Span`, `SyntaxSymbol`,
  `ParsedSyntax`) — same rule as every other result type in the codebase
- Byte-span slicing: node offsets are byte offsets into the captured UTF-8
  bytes, and all text leaves the module through `slice_text(source, start,
  end)` — never `str` indexing, which would corrupt multibyte positions
- Extraction walks the tree with an explicit stack of sibling-iteration
  frames, never Python recursion — a deeply nested file cannot exhaust the
  interpreter's recursion limit

### 14. Staged Publication

**Pattern:** `index_repo()` runs fixed stages — validate, syntax baseline,
optional SCIP, Zoekt, optional semantic, revalidate + graph, then
**publish → record → retire** in that strict order — and each stage owns its
failure boundary. Expected SCIP-stage failures *degrade* (exit-0 `degraded`,
cause recorded); Zoekt/storage/publication failures fail the run with nothing
new published.

**Why:**
- The optional enrichment can never block the build-free baseline (spec TSI-01)
- Ordering publish before record before cleanup means a crash at any point
  leaves either the old or the new snapshot live — never an orphaned cleanup
  or a destroyed live snapshot
- One immutable `index-<sha>-<generation>.db` per run, selected by one
  `current` pointer, keeps readers generation-consistent: a single tool
  operation cannot mix tables from two generations

**Convention:** Scratch build → unique final name (`uuid4().hex` generation)
→ write-temp-then-rename pointer flip → registry terminal write → retire
superseded snapshots (best-effort, warns only). Never mutate a published
database; never derive cleanup targets from anything but the filenames being
deleted.

### 15. Registry Status Vocabulary

**Pattern:** Status strings are module-level constants in `registry.py`
(`INDEXING_STATUS`, `PARTIAL_STATUS`, `DEGRADED_STATUS`, plus the `SCIP_STATES`
frozenset), not inline literals. The CLI writer and the MCP reader import the
same constants, so the vocabularies cannot drift.

**Why:**
- `jarvis status` output and `getIndexStatus` payloads must spell states
  identically; duplicated string literals drifted exactly this way before
- A frozen vocabulary makes read-time normalization honest: an unrecognized
  persisted `scip_state` reads back as `unknown` instead of propagating

**Convention:** Add a new state by extending the constant in `registry.py`
first, then the readers — never by writing a new literal at a call site.

---

## Code Organization

### Module Docstrings

Every module has a docstring explaining its purpose and key exports. Example:

```python
"""SCIP blob decoder (zstd+protobuf); isolation seam for protobuf dependency.

Decode zstd+protobuf `scip.Document` occurrences and `global_symbols.relationships`.
Also parses symbol packages and range→position conversions.

Known gap: SCIP v0.9.0 converter never populates `relationships`,
so `typeHierarchy` is empty on real indexes (upstream issue scip-code/scip#464).
"""
```

**Convention:** Module docstrings should state purpose, key functions, and known gaps/limitations.

---

### Single-Purpose Functions

**Pattern:** Small, testable functions with clear contracts.

**Examples:**
- `repo_slug(name: str) -> str` — normalize user input, reject traversal attacks
- `scip_range_to_positions(range) -> (line, character)` — convert SCIP to LSP coordinates
- `should_ignore_path(path) -> bool` — centralized exclusion list
- `hash_file(data: bytes) -> str` (`chunker.py`) — content hash used for chunk dedup and
  carry-over-unchanged-files detection
- `language_for(path: Path) -> str | None` (`chunker.py`) — maps a file extension to its
  tree-sitter grammar, or `None` to fall back to fixed-window chunking

**Convention:** No "god functions" combining multiple concerns. If a function grows beyond ~50 lines, consider splitting.

---

## CLI Design

### Command Structure

All commands are under `jarvis`:

```bash
jarvis index <path> [--slug name] [--scheme name] [--language name] [--semantic-include path] [--scip | --no-scip]
jarvis list
jarvis status <slug>
jarvis reindex <slug> [--scip | --no-scip]
jarvis forget <slug>
jarvis watch <path> [--slug name] [--scheme name] [--language name] [--semantic-include path] [--debounce 5] [--scip | --no-scip]
```

### Error Handling

**Pattern:** CLI errors print to stderr with context (which repo failed, why) and exit non-zero.

**Examples:**
- `f"Repo {repo} not indexed"`
- `f"Symbol not found in {path} at {line}:{character}"`
- `f"Failed to populate graph for {repo}: {e}"`

**Convention:** Error messages are user-facing (appear in MCP responses). Make them actionable.

---

## Testing Conventions

### Test File Organization

Each test file mirrors its source module:
- `test_query.py` → `query.py`
- `test_graph.py` → `graph.py`
- `test_chunker.py` → `chunker.py`
- `test_embeddings.py` → `embeddings.py`
- `test_semantic.py` → `semantic.py`
- etc.

(`models.py` and `__init__.py` are the only modules without a dedicated test file — see Test Coverage below.)

### Unit vs Integration Tests

**Unit tests:**
- Mock external dependencies (file I/O, subprocess calls, external HTTP)
- Use fixtures for synthetic SCIP blobs and SQLite schemas
- Mark with no special marker (run by default)

**Integration tests:**
- Call real binaries (scip-python, scip, zoekt-index, zoekt-webserver)
- Marked `@pytest.mark.integration`
- Concentrated in `test_index_cli.py` (the full pipeline)

**Run tests:**
```bash
uv run pytest                    # all tests
uv run pytest -m "not integration"  # unit only
uv run pytest -m integration     # real binaries only
```

**Convention:** Mark integration tests explicitly. Don't surprise developers with subprocess calls in unit tests.

---

### Fixtures

**Location:** `tests/fixtures/`

**Key fixtures:**
- `mini_py_repo/greeter.py` — Minimal Python file for scip-python indexing tests
- `scip_encoder.py` — Real zstd+protobuf SCIP blob builders (deterministic, repeatable)
- `synthetic_index.py` — Hand-copied real SQLite schema (documents/chunks/global_symbols tables with sample data)

**Convention:** Fixtures contain real, reproducible data (not random). Blob fixtures are compressed and can be inspected with zstandard tools.

---

## CLI Design

### Command Structure

All commands are under `jarvis`:
```bash
jarvis index <path> [--slug name] [--scheme name] [--language name] [--semantic-include path]
jarvis list
jarvis status <slug>
jarvis reindex <slug>
jarvis forget <slug>
jarvis watch <path> [--slug name] [--scheme name] [--language name] [--semantic-include path] [--debounce 5]
```

### Error Handling

CLI errors are printed to stderr with context (e.g., which repo failed, why).

```python
try:
    index_repo(path, slug)
except Exception as e:
    print(f"Error indexing {path}: {e}", file=sys.stderr)
    sys.exit(1)
```

**Convention:** Always exit with non-zero code on error. Include the repo/path in the error message.

---

## Documentation Standards

### Module Docstrings

Every module has a docstring (see above). Include:
- Purpose (1 sentence)
- Key exports / responsibilities
- Known gaps (upstream limitations, not implemented features)

### Function Docstrings

Functions with non-obvious behavior have docstrings:

```python
def repo_slug(name: str) -> str:
    """Normalize a user-chosen repo name into a directory-safe slug.

    Rejects `.`/`..` explicitly (not just `/`) — both survive the
    character-class substitution below unchanged since `.` is an allowed
    slug character, but either one alone is a path-traversal component.
    """
```

**Convention:** Use present tense ("normalizes", "validates"). Include edge cases (like `.` / `..` rejection).

### Comments

Comments explain *why*, not *what*. Code is readable; comments should justify decisions.

Example (good):
```python
# We use mode=ro&immutable=1 to enable WAL safety on NFS
# (vendored from source project's index_reader.py)
conn = sqlite3.connect(path, uri=True)
```

Example (bad):
```python
# Open the database
conn = sqlite3.connect(path, uri=True)
```

---

## Naming Conventions

### Modules
- Lowercase, snake_case: `index_reader.py`, `scip_decoder.py`

### Classes & Dataclasses
- PascalCase: `QueryService`, `SymbolInfo`, `IndexConnectionCache`

### Functions & Methods
- snake_case: `repo_slug()`, `index_repo()`, `blast_radius()`

### Constants
- UPPER_CASE: `PROJECT`, `BRANCH`, `DEFAULT_DATA_DIR`

### Private Functions & Attributes
- Prefix with `_`: `_service()`, `_query_service`, `_json_safe()`

### Environment Variables
- UPPER_CASE, prefixed with `JARVIS_`: `JARVIS_DATA_DIR`, `JARVIS_ZOEKT_BIN`,
  `JARVIS_EMBEDDING_MODEL`, `JARVIS_EMBEDDING_BATCH_SIZE`,
  `JARVIS_EMBEDDING_QUERY_PREFIX`, `JARVIS_EMBEDDING_DOC_PREFIX`

---

## Performance & Scalability

### Single-Threaded Query Path

The entire query path (from MCP tool → SQL → result) is synchronous and single-threaded. MCP clients are responsible for parallelization.

**Convention:** Don't add async/await unless blocking I/O becomes a bottleneck. jarvis is a single-user tool; no need for concurrent client handling.

### Connection Pooling

`IndexConnectionCache` maintains a bounded pool of SQLite connections per unique (project, repo, branch, pointer_content) tuple, garbage-collected on pointer changes.

**Convention:** Reuse the cache for all queries; never open raw sqlite3 connections in query logic.

---

## Type Hints

**Convention:** Use modern syntax (Python 3.10+):
- `str | None` instead of `Optional[str]`
- `list[T]` instead of `List[T]`
- `dict[K, V]` instead of `Dict[K, V]`

Full type hints on all public functions; private/internal functions may omit hints if obvious from context.

---

## Future-Proofing

### SCIP Version Pinning

SCIP proto is pinned to **v0.9.0** in `scip_pb2.py` (regenerated from v0.7.0 because v0.7.0 lacked the `typed_range` oneof that `scip-swift` requires).

**Convention:** Document SCIP version in README and code. Any future version bump should be tracked in a plan, not a surprise refactor.

### SQLite Schema Versioning

The `scip expt-convert` output schema is not versioned. If scip releases change the schema, jarvis will need to adapt query logic.

**Convention:** Document which `scip` release was tested. Add comments to SQL queries if they depend on specific schema columns.

### Zoekt Server Lifespan

`ZoektLifecycle` manages the `zoekt-webserver` process (lazy-start, pidfile-tracked, killed on exit).

**Convention:** The lifecycle is encapsulated in `ZoektLifecycle`; don't spawn zoekt-webserver elsewhere. Always check if it's running before querying.

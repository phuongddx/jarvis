# jarvis: Codebase Summary

## Directory Structure

```
jarvis/
├── src/jarvis/           # Core library (16 files)
├── tests/                   # Test suite (16 files)
├── docs/                    # Documentation
├── plans/                   # Implementation plans
├── .github/workflows/       # CI: build-scip, build-zoekt, publish-native,
│                            #     setup-smoke, test
├── scripts/                 # Version/native release utilities
│   └── check_versions.py    # Sole release-version readability guard
├── setup.sh                 # Optional language-indexer bootstrapper
├── SCIP_COMMIT / ZOEKT_COMMIT # Pinned native binary source commits
├── pyproject.toml           # uv-managed project config
└── README.md                # User-facing getting started
```

## Core Modules (`src/jarvis/`)

### Initialization & Configuration

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `__init__.py` | 2 | Stub entry; unused (`main()` prints "Hello from jarvis!") | — |
| `config.py` | 66 | Data-dir + repo-slug resolution; single-tenant layout; shared `IGNORED_DIRS` constant; `lancedb_dir()` for the semantic vector store | `data_dir()`, `repo_slug()`, `index_dir()`, `lancedb_dir()`, `IGNORED_DIRS`, `PROJECT`, `BRANCH`, `DEFAULT_DATA_DIR` |
| `models.py` | 64 | Frozen dataclasses for nav results (Position, Range, Location, SymbolInfo, etc.) + Freshness StrEnum | `Position`, `Range`, `Location`, `SymbolInfo`, `DocumentSymbolEntry`, `CallHierarchyEntry`, `TypeHierarchyEntry`, `Freshness` |

### Index & Search

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `index_reader.py` | 160 | Vendored filestore reader from SCIP source; `IndexConnectionCache` — thread-safe, size-bounded cache of read-only immutable SQLite connections keyed by `(project, repo, branch, pointer_content)`, NFS-safe pointer invalidation | `IndexConnectionCache`, current pointer file handling |
| `scip_pb2.py` | 119 | Generated protobuf from scip.proto v0.9.0 — regenerated from v0.7.0 because v0.7.0 lacked the `typed_range` oneof that `scip-swift` requires (do not edit, vendored codegen) | scip.Document, scip.SymbolInformation, scip.Occurrence, scip.Relationship |
| `scip_decoder.py` | 326 | SCIP blob decoder (zstd+protobuf); isolation seam for protobuf dependency | `scip_range_to_positions()`, `kind_name()`, `parse_symbol_package()`, decode SCIP occurrences + relationships |
| `query.py` | 462 | QueryService: 5 nav ops with per-file provider routing (SCIP outline/definitions when the file has coverage, else Tree-sitter syntax declarations from the same snapshot), SCIP-only tools raising `CapabilityUnavailableError` (`requiredCapability`/`reason`/`recovery`), `getIndexStatus` facts + `FreshnessSnapshot` carrying the snapshot `generation` | `QueryService`, `CapabilityUnavailableError`, `FreshnessSnapshot`, nav result builders |
| `syntax.py` | — | Curated offline Tree-sitter grammar provider (`FACTORIES` maps 17 internal language names to 16 pinned grammar distributions; repository-supplied grammar code is never instantiated), `ParserPool` (per-worker parser cache), declaration extraction with an explicit non-recursive walk, byte-span slicing, deterministic opaque `syntax:` ids | `ParserPool`, `language_for_path()`, `extract_file()`, `grammar_identity()`, `SyntaxSymbol`, `Span` |
| `syntax_index.py` | — | Immutable syntax/provider snapshot builder: git-tracked source capture/validate, incremental build keyed on file hash + `grammar_identity`, namespaced `syntax_files`/`syntax_symbols`/`jarvis_snapshot` schema, per-file SCIP provider-coverage derivation, snapshot facts reads | `capture_sources()`, `validate_sources()`, `build_syntax_index()`, `finalize_snapshot()`, `read_snapshot_facts()`, `file_symbols()`, `find_syntax_symbols()`, `file_coverage()`, `file_provider_coverage()` |
| `search.py` | 183 | `searchCode` backend via real httpx client to zoekt-webserver; `ZoektLifecycle` lazy-spawns `zoekt-webserver -rpc`, pidfile-tracked | `searchCode()`, `ZoektLifecycle` |
| `chunker.py` | 394 | Tree-sitter AST chunking into function/class-sized chunks (256-512 token target) with byte-safe Unicode slicing (`CONTENT_FORMAT = 2`), accepts a supplied parse `tree` so the syntax baseline's parse is reused instead of reparsed, fixed-window fallback for unparseable languages, content-hash dedup; admission filters (generated-file banner/long-line, `.gitignore` via batched `git check-ignore`, 1 MB size cap, `--semantic-include` escape hatch); appends a `# file:`/`# in class:` context header to every chunk as a final pass | `chunk_file()`, `Chunk`, `hash_file()`, `language_for()`, `skip_reason()`, `iter_source_files()`, `gitignored()`, `oversized_file_reason()`, `CONTENT_FORMAT` |
| `embeddings.py` | 149 | Lazy-loaded self-hosted embedding model wrapper (`BAAI/bge-m3`, 1024-dim, pinned revision), L2-normalized vectors; model-aware query/doc instruction prefixes (`MODEL_PREFIXES`, env-overridable); `SemanticExtraMissingError` for clean skip when the `semantic` extra isn't installed | `EmbeddingModel`, `default_model()`, `SemanticExtraMissingError` |
| `semantic.py` | 340 | `SemanticStore` (one LanceDB table per repo), `prepare_semantic()`/`finish_semantic()` split of the old `index_semantic()` (chunk reusing the syntax stage's parse input, then encode + publish), `reciprocal_rank_fusion()` (k=60), `semantic_search()`; `TableIdentity` (model + revision + prefixes + content format) gates carry-forward reuse | `SemanticStore`, `prepare_semantic()`, `finish_semantic()`, `semantic_search()`, `reciprocal_rank_fusion()`, `TableIdentity`, `TokenStats` |

### Graph & Registry

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `graph.py` | 363 | Package dependency graph: sqlite3 CRUD on `packages`/`edges` tables in registry.db, `populate_graph_for_repo()` (rebuild-not-accumulate), `blast_radius()` 2-hop BFS | `GraphStore`, `extract_package_names()`, `populate_graph_for_repo()`, `blast_radius()` |
| `registry.py` | 256 | sqlite3 CRUD on `repos`: slug/path/language/commit_sha/last_indexed and the TSI-06 status vocabulary (`indexing`/`indexed`/`partial`/`degraded`/`failed`), persisted SCIP stage columns (`scip_enabled`, `scip_state` ∈ available/partial/failed/unavailable/unsupported/disabled, failure reason/stderr, `scip_failed_at_sha`, `status_origin`/`reason`/`stderr`), `scheme_override`/`semantic_include`/`language_override`/`semantic_indexed_at`/`semantic_declined` persistence, `recovery_for()` derived retry commands, and the one-time idempotent migration (TSI-08) from the superseded search-only columns | `Registry`, repo table operations, `mark_semantic_indexed()`, `SCIP_STATES`, `recovery_for()`, idempotent migrations |

### Server & CLI

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `server.py` | — | MCP stdio server entry (`FastMCP("jarvis")`), registers 10 tools with thin wrappers around QueryService/ZoektLifecycle/GraphStore/semantic, uniform `{"error": ...}` error payload (`CapabilityUnavailableError` renders `requiredCapability`/`reason`/`recovery`); `getIndexStatus` adds `capabilities.tools` (per-tool providers for all five nav tools), `capabilities.syntax` (extraction counts + extraction identity), the snapshot `generation` in freshness, and an `indexing` block while a run is in flight | MCP tool handlers: `documentSymbols`, `goToDefinition`, `findReferences`, `callHierarchy`, `typeHierarchy`, `getIndexStatus`, `searchCode`, `semanticSearch`, `blastRadius`, `indexRepo` |
| `index_cli.py` | — | The `jarvis` CLI (`index`, `list`, `status`, `reindex`, `forget`, `watch`): `index_repo()` staged pipeline (validate → capture + syntax baseline → optional SCIP gated on the persisted `--scip`/`--no-scip` choice and the four-condition watch suppression predicate (`_scip_suppressed`) → zoekt → optional semantic (`prepare`/`finish` split) → revalidate + graph edges → publish/record/retire in strict order), `index-<sha>-<generation>.db` artifacts + atomic `current` pointer flip (`_publish_atomically`), removed-flag rejection (`--search-only` & co. fail loudly with their replacement), xcodebuild build-tool selection for Swift repos (`_prefers_xcodebuild()`, `_swift_indexer_cmd()`), and `forget` teardown (graph edges, zoekt pin + shards, artifacts) | `index_repo()`, `main()`, `build_parser()`, `detect_language()`, `_scip_suppressed()`, `_resolve_scip_enabled()`, `_publish_atomically()` |
| `jobs.py` | — | in-flight index-run coordination — per-slug `flock` build lock, write-once launch record, ordered job-state derivation; shared by the CLI writer and the MCP reader. | `build_lock()`, `lock_state()`, `write_launch_record()`, `read_launch_record()`, `job_state()`, `clear_job_files()`, `LaunchRecord`, `JobState` |
| `watch.py` | 55 | `Debouncer` (pure, thread-free, injectable clock) + `should_ignore_path` (.git/node_modules/.venv/__pycache__/dist/build) | `Debouncer`, `should_ignore_path()` |

### Root-Level Files

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `setup.sh` | ~760 | POSIX-sh optional bootstrapper: installs one retained language indexer or the macOS Java bash shim into `~/.jarvis/bin` | `--only scip-swift|scip-typescript|scip-python|scip-java|bash-shim`, `--force` |
| `ZOEKT_COMMIT` | 1 | Pinned upstream `sourcegraph/zoekt` commit that CI cross-compiles | — |

## Test Suite (`tests/`)

### Test Files (1:1 map to src modules)

| Test File | Covers | Scope |
|-----------|--------|-------|
| `test_config.py` | config.py | Data-dir resolution, slug sanitization |
| `test_index_reader.py` | index_reader.py | Vendored cache behavior, connection pooling |
| `test_scip_decoder.py` | scip_decoder.py | Blob decoding, range/symbol parsing |
| `test_query.py` | query.py | SQL execution, nav result builders |
| `test_search.py` | search.py | Zoekt HTTP client, lifecycle management |
| `test_chunker.py` | chunker.py | AST chunking, fixed-window fallback, dedup hashing |
| `test_syntax.py` | syntax.py | Real ParserPool parsing (all 17 languages), declaration extraction rules, byte-span spans, `syntax:` id stability, `SyntaxDependencyError` on missing grammar |
| `test_syntax_index.py` | syntax_index.py | Capture/validate, incremental reuse, schema + provider-coverage derivation, snapshot facts, publish |
| `test_embeddings.py` | embeddings.py | Lazy model loading, normalization, missing-extra error |
| `test_semantic.py` | semantic.py | SemanticStore CRUD, index_semantic(), reciprocal_rank_fusion() |
| `test_graph.py` | graph.py | Dependency graph CRUD, blast_radius BFS |
| `test_registry.py` | registry.py | Registry table operations, status updates |
| `test_watch.py` | watch.py | Debouncer logic, path filtering |
| `test_server_tools.py` | server.py | MCP tool payloads, error handling |
| `test_index_cli.py` | index_cli.py | Full pipeline (e-2-e); marked `@pytest.mark.integration` — calls real scip-python/scip/zoekt binaries |
| `test_index_status.py` | index_cli.py + query.py | Freshness snapshot, staleness detection |
| `test_setup_sh.py` | setup.sh | Sources the script under `dash` (not `sh` — macOS `/bin/sh` accepts bashisms) and tests each function in isolation |
| `test_check_versions.py` | scripts/check_versions.py | Reads the sole release version from `pyproject.toml`; plugin manifests live in jarvis-index and version independently |

### Fixtures (`tests/fixtures/`)

| Fixture | Purpose |
|---------|---------|
| `mini_py_repo/greeter.py` | Minimal Python file for integration tests |
| `mini_swift_repo/` | Minimal Swift repo (Package.swift + Sources/MiniSwiftRepo/Greeter.swift) for xcodebuild detection tests |
| `scip_encoder.py` | Real zstd+protobuf SCIP blob builders (synthetic index) |
| `synthetic_index.py` | Hand-copied real SQLite schema fixture (documents/chunks/global_symbols, etc.) |

## Key Patterns

### Dataclass-First Design
All result types (`Position`, `Range`, `Location`, `SymbolInfo`, etc.) are frozen dataclasses, not Pydantic. Server.py converts them to dicts via `dataclasses.asdict()` for MCP payloads. No validation layer because the runtime has no HTTP boundary.

### SCIP Decoder Isolation Seam
`scip_decoder.py` is the only module importing `scip_pb2` and `zstandard`. This allows future SCIP proto version bumps to be localized.

### Atomic Pointer-Swap Publish
Index publishing writes a new versioned database, waits for graph/Zoekt completion, then atomically swaps the `current` pointer file via `os.replace()`. Queries reading the old index are never interrupted.

### Rebuild-Not-Accumulate Graph
`populate_graph_for_repo()` clears that repo's outgoing edges before recomputing; removed dependencies are retracted. The graph is always current, never stale.

### Broad Exception Handling by Design
`server.py` catches all exceptions and returns `{"error": "..."}` payloads — not passing exceptions to the MCP transport. This keeps the server alive even on query bugs.

### ENV Var Overrides
- `JARVIS_DATA_DIR` — override default `~/.jarvis`
- `JARVIS_ZOEKT_BIN` — override zoekt-webserver binary path (default: `zoekt-webserver`)

### Single-Tenant Hardcoding
`config.py` pins `PROJECT = "_"` and `BRANCH = "_"` — the vendored `IndexConnectionCache` keys on 3-tuples, but jarvis has no project/branch concept. The disk path `scip/_/<slug>/_/` is an artifact of reusing the cache's path shape unchanged.

## Test Coverage

- **Unit tests** cover all modules except `__init__.py` (dead stub) and `models.py` (trivial frozen dataclasses)
- **Integration tests** (marked `@pytest.mark.integration`) run real SCIP indexers, `scip expt-convert`, and `zoekt-git-index` on a mini Python repo
- **Run tests:** `uv run pytest` (all), `uv run pytest -m "not integration"` (unit only), `uv run pytest -m integration` (real binaries only)

## CI Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `.github/workflows/build-scip.yml` | `SCIP_COMMIT` change or manual dispatch | Cross-compiles the forked `scip` converter for four platform/arch pairs and publishes it to public `jarvis-intelligence/jarvis-index` releases for native packaging |
| `.github/workflows/build-zoekt.yml` | `ZOEKT_COMMIT` change or manual dispatch | Cross-compiles `zoekt-git-index`/`zoekt-webserver` for four platform/arch pairs and publishes them to public `jarvis-intelligence/jarvis-index` releases for native packaging |
| `.github/workflows/publish-native.yml` | GitHub Release published | Builds/smoke-tests four standalone archives, uploads archive+checksum assets to the public tap release, renders the checksum-pinned formula, validates it on four Homebrew environments, then commits `Formula/jarvis.rb` to `jarvis-intelligence/homebrew-jarvis` |
| `.github/workflows/setup-smoke.yml` | `setup.sh`/test/workflow changes, PRs, manual | Parses `setup.sh` and runs `tests/test_setup_sh.py`; no live installer side effects |
| `.github/workflows/test.yml` | Every push/PR | Runs unit test suite (`pytest -m "not integration"`); installs `semantic` extra so `test_semantic.py` tests actually run; no path filter (runs on every change) |

## Dependencies & Imports

- **Source runtime:** mcp[cli], protobuf, zstandard, httpx, tree-sitter + 16 curated grammar packages (base deps), watchdog (optional extra, bundled in the standalone archive)
- **No ORM:** Direct sqlite3 usage throughout
- **No async framework:** Pure sync code, single-threaded query path
- **Minimal third-party:** ~150 lines of pure-dataclass models, ~200 lines of CLI glue

## Module Call Graph (Key Paths)

**Indexing pipeline (`index_cli.py`):**
1. Validate git input, slug ownership, persisted config (SCIP tooling deliberately not validated — it is optional)
2. `syntax_index.capture_sources()` → `build_syntax_index()` (extract or reuse declarations into a scratch snapshot)
3. Optional SCIP attempt (`_attempt_scip`): language indexer → `scip expt-convert`; gated on `_resolve_scip_enabled()` + `_scip_suppressed()`; expected failures degrade to exit-0 `degraded`
4. `zoekt-git-index` → shards in `.zoekt/` (failure fails the run)
5. `_prepare_semantic_stage()`/`_finish_semantic_stage()` — chunk (reusing the syntax parse), embed, store (non-fatal)
6. `validate_sources()` + graph edge update (`populate_graph_for_repo()`, or edge-clearing for a syntax-only generation)
7. Publish `index-<sha>-<generation>.db` + metadata sibling via `_publish_atomically()` (`os.replace` pointer flip) → registry terminal write → `_retire_superseded_snapshots()` — strictly in that order

**Query path (`server.py` → `query.py`) — the per-file routing seam:**
1. MCP tool handler unpacks `repo`, `symbol`/`path` args
2. `QueryService._connection()` opens `index-<sha>-<generation>.db` read-only (cache keyed on pointer content)
3. `read_snapshot_facts()` / `file_provider_coverage()` decide, per file and per operation, SCIP vs. Tree-sitter: SCIP-covered files use `documents/global_symbols/mentions`; uncovered files use `syntax_symbols` rows; bare names resolve across both providers (combined candidates)
4. Build result dataclasses (`Location.source` = `"scip"` | `"tree-sitter"`, `positionEncoding`, syntax entries add `selectionRange`/`qualifiedName`/`parentSymbol`; `documentSymbols` adds a `coverage` object)
5. SCIP-only tools (`findReferences`/`callHierarchy`/`typeHierarchy`) with no usable capability raise `CapabilityUnavailableError` → `requiredCapability`/`reason`/`recovery` payload, never an empty array
6. MCP handler converts to dict, returns the payload or `{"error": ...}`

**Search path (`server.py` → `search.py`):**
1. First call to `searchCode` → `ZoektLifecycle.ensure_running()` spawns `zoekt-webserver -rpc` (pidfile-tracked)
2. HTTP POST to `http://localhost:PORT/api/search` with query
3. Parse JSON response, wrap in Zoekt result types
4. Return to MCP client
5. Server exit → `atexit` handler kills zoekt-webserver

**Graph path (`server.py` → `graph.py`):**
1. `blastRadius(repo, symbol_or_package)` → lookup package name
2. BFS up to 2 hops in `edges` table (outgoing edges from that package)
3. For each dependent, fetch repo info from registry
4. Return list with hop distances

**Source-build semantic path (`server.py` → `semantic.py`; excluded from Homebrew):**
1. `semanticSearch(repo, query, limit=10)` → embed query with the table's recorded `TableIdentity` (model, revision, and prefixes)
2. Vector search the repo's LanceDB table (cosine metric)
3. `symbol_search.search_symbols()` matches query tokens against the repo's SCIP symbol table (when a SCIP index exists) and resolves ranked candidates to definition locations
4. `reciprocal_rank_fusion()` merges vector hits, `searchCode`'s Zoekt lexical hits, and the symbol hits (k=60, all three signals unweighted)
5. Return ranked hits; include a `"warning"` if the table's identity (model/revision/prefixes/content format) differs from the currently configured one

## Size Profile

- **Total LOC (src):** see `wc -l src/jarvis/*.py` for current counts — the tree-sitter syntax baseline added `syntax.py` (~1.1k LOC) and `syntax_index.py` (~850 LOC) and rewrote `index_cli.py`'s pipeline around the staged publish
- **Largest module:** `index_cli.py`
- **Smallest module:** `__init__.py` (2 LOC)

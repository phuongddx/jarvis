# jarvis: Project Overview & PDR

## What Is jarvis?

**jarvis** is a personal, local-first code intelligence MCP server that brings structural code navigation (SCIP-backed) and lexical search (Zoekt-backed) to Claude Code, Cursor, and any MCP client. Source builds can add natural-language semantic/vector search; the Homebrew distribution excludes that path. jarvis runs as a single stdio process — no server, no auth, no network.

**Core value proposition:**
- **Structural navigation** (SCIP) at your fingertips: go-to-definition, find-references, call/type hierarchy, document symbols
- **Lexical search** with Zoekt: index your repos once, search instantly across all indexed code
- **Source-build semantic/vector search:** ask in plain English (`semanticSearch`) and get tree-sitter-chunked code ranked by a self-hosted embedding model, fused with lexical hits and SCIP symbol-definition matches — bridges toward SCIP's structural navigation above rather than being fully distinct from it
- **Single-user, local:** read-only runtime, atomic publish guarantees, privacy-by-default
- **Minimal dependencies:** stdlib sqlite3, plain dataclasses, no ORMs or async framework bloat

## Who Is It For?

- **Personal developers** working with multiple repositories and wanting MCP-integrated code intelligence
- **Users of Claude Code / Cursor** who want local structural and lexical results without cloud services
- **Teams / enterprises** (future, Phase 5+) — roadmap planned; not in current scope

## Scope

### In Scope (Shipped, All 4 Phases Complete)

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Scaffold + vendored SCIP core (`scip_pb2`, `scip_decoder`, `index_reader`) | ✓ Done |
| 2 | MCP stdio server + 5 SCIP nav tools + `getIndexStatus` | ✓ Done |
| 3 | Indexer CLI (`jarvis index`), registry, embedded Zoekt + `searchCode` | ✓ Done |
| 4 | `blastRadius` (package dependency graph) + `jarvis watch` (auto-reindex) | ✓ Done |
| Post-Phase-4 | Semantic/vector search (`semanticSearch`): tree-sitter chunking, self-hosted embeddings, per-repo LanceDB store, fused with Zoekt via reciprocal rank fusion. Source builds gate it behind the optional `semantic` extra; the standalone distribution excludes it | ✓ Done |

**Language support:** TypeScript, Python, Java/Kotlin, and Swift — `.swift` repos are recognized by `detect_language()` and routed to [`scip-swift`](https://github.com/phuongddx/scip-swift), which builds and indexes end-to-end. Navigation tools return correct results on real Swift repos. Requires a macOS host (Xcode + iOS SDK) for repos importing Apple-platform frameworks. One language per index; language detection by file-extension plurality across git-tracked files. Pass `--language <name>` to override detection.

**Java/Kotlin caveat:** SCIP navigation is supported for plain JVM Gradle/Maven repos with `scip-java`, but two cases publish search-only instead: Android/AGP projects (scip-java's Gradle plugin relies on standard source sets that AGP replaces with variants, producing zero SCIP shards upstream scip-java#177); and Kotlin versions other than the pinned release (scip-kotlinc is compiled against exactly one Kotlin version — others fail with AbstractMethodError/NoSuchMethodError). Search-only provides Zoekt lexical search (plus source-build semantic search when enabled), with no SCIP navigation.

**10 MCP tools:** `documentSymbols`, `goToDefinition`, `findReferences`, `callHierarchy`, `typeHierarchy`, `getIndexStatus`, `searchCode`, `semanticSearch`, `blastRadius`, `indexRepo`

### Out of Scope (Explicitly Not Planned)

- **Cloud deployment** (Phase 5 in original brainstorm; deprioritized indefinitely)
- **Stats/dashboard endpoints** — jarvis is a query engine, not an analytics backend
- **Monorepo language-merge polish** — multi-language indexing would require schema refactoring not justified by single-user use case
- **GUI** — CLI + MCP tools only
- **Incremental indexing** — full rebuild on each `index` / `watch` reindex
- **Custom symbol filtering / search-rank tuning** — delegates to SCIP and Zoekt as-is
- **Semantic dependencies in the standalone distribution** — `semanticSearch` remains registered but reports Homebrew-specific unavailability

## Architectural Guarantees

**Read-only runtime:** Query operations open published indexes read-only (`mode=ro&immutable=1`). The runtime never mutates index files.

**Atomic publishing:** Reindex writes a new versioned `.db`, populates the package graph, and runs `zoekt-git-index`. Only once all succeed does `os.replace` flip the small `current` pointer. A query already reading the old file keeps working; zero downtime, no partial-state windows.

**Rebuild-not-accumulate graph:** Each reindex clears that repo's outgoing package dependencies before recomputing them. Removed dependencies are retracted. The graph always reflects each repo's *last* index run, not an accumulation.

## Dependencies

Core runtime:
- Python 3.12+
- `mcp[cli]` — FastMCP for stdio server
- `protobuf` — scip_pb2 message decoding
- `zstandard` — SCIP blob decompression
- `httpx` — Zoekt webserver client
- `tree-sitter` plus the curated grammar packages — syntax baseline and semantic chunking
- `watchdog` — source extra for `jarvis watch`; included in the standalone archive

Source-only, gated behind `--extra semantic` (not required for the base install):
- `lancedb` — per-repo vector table storage
- `sentence-transformers` — self-hosted embedding model (`BAAI/bge-m3` by default)
- Tree-sitter AST source chunks (base grammars above)

End-user native package:
- Embedded Python 3.12, `jarvis`, `jarvis-server`, dashboard assets, tree-sitter, `watch`, `scip`, `zoekt-git-index`, and `zoekt-webserver`
- `universal-ctags` supplied by Homebrew
- Optional language indexers: `scip-typescript` and `scip-python` (Node.js/npm), `scip-java` (JDK; Kotlin 2.2.0 exactly), or `scip-swift` (Xcode on macOS arm64), each installed with its `setup.sh --only ...` selector

## Entry Points

| Command | Module | Purpose |
|---------|--------|---------|
| `jarvis` | `index_cli.py` | Indexing, registry, watch |
| `jarvis-server` | `server.py` | MCP stdio server |

## Distribution

jarvis is available to end users through one installation channel:

**Homebrew standalone:** `brew install jarvis-intelligence/jarvis/jarvis` on macOS arm64/x86_64 or Linux arm64/x86_64 via Homebrew/Linuxbrew. No Python, uv, pip, or package index is required. `brew upgrade jarvis` upgrades an existing install; `brew reinstall jarvis` recovers a damaged one. The generated formula is published to `jarvis-intelligence/homebrew-jarvis` by `publish-native.yml`.

**Claude Code plugin:** After the Homebrew binary is installed, `/plugin marketplace add jarvis-intelligence/jarvis-index` and `/plugin install jarvis@jarvis` add jarvis skills and register the MCP server. Source of truth is the jarvis-index repo: plugin manifest and MCP registration live there; skills are under `plugin/skills/`.

**Codex plugin:** `.codex-plugin/plugin.json` (jarvis-index root) declares the same `plugin/skills/` tree for Codex, adding the interface metadata Codex requires (display name, category, capabilities, default prompts, icons) that the Claude manifest does not carry. Both manifests point at one shared skills directory, so a skill is authored once and served to both hosts.

**Version consistency:** `pyproject.toml [project].version` is the sole local release declaration and is checked by `scripts/check_versions.py` in CI. A release also runs `uv lock` so the lockfile records the new project version. The Claude Code and Codex plugin manifests live in jarvis-index and version independently.

## Database Schema

**Indexing:**
- `registry.db` — repos table (slug/path/language/commit_sha/last_indexed/status/scheme_override/semantic_indexed_at/semantic_include/language_override); packages/edges tables (dependency graph)
- Per-repo: `index-<sha>.db` (from `scip expt-convert`) — documents/chunks/global_symbols/mentions/defn_enclosing_ranges
- Zoekt shards: `.zoekt/` directory (spawned lazily)
- `~/.jarvis/lancedb/` — one LanceDB vector table per repo (source-build semantic search only)

**Current pointer:** Small `current` file per repo, atomically updated on successful publish.

## Success Criteria (All Met)

- From Claude Code (user-scope MCP), on real TypeScript and Python repos: the MCP tools return correct results; nav results hand-verified on known symbols ✓
- `jarvis index <repo>` end-to-end: detect language → run language indexer → `scip expt-convert` → zoekt-git-index → atomic pointer swap → registry update ✓
- `getIndexStatus` correctly flags stale after new commits; reindex has zero query downtime ✓
- Test suite: all phases gate on `uv run pytest` green (12 test modules, 1-1 map to src modules except `__init__.py`/`models.py`, plus fixtures with real SCIP/Zoekt blobs — 17 files total under `tests/`) ✓

## Non-Goals / Known Limitations

**Upstream (not bugs):**
- An unpatched upstream `scip expt-convert` omits `global_symbols.relationships` ([scip-code/scip#464](https://github.com/scip-code/scip/issues/464), fixed by [PR #465](https://github.com/scip-code/scip/pull/465)); the native package uses the fork build so `typeHierarchy` is answerable
- Zoekt `repo` filter matches directory basename, not jarvis slug — may diverge if `--slug` was passed
- **Java/Kotlin gaps** (see "Language support" above): Android/AGP projects produce no SCIP shards (upstream scip-java#177), and Kotlin versions other than the pinned release fail to compile with scip-kotlinc (compiler-plugin API is internal/unstable) — both trigger automatic search-only publishing with no SCIP navigation

**By design:**
- No per-node timestamp on the package graph → `blastRadius` always reports `freshness: "unknown"`
- Cross-repo dependency edges resolve by exact package name — index dependencies first, or re-run `jarvis index` after indexing them, for edges to appear
- Single-user only; no auth, no multi-tenant schema
- No config file; env vars + CLI flags only

## Standards & Compliance

- **SCIP protocol:** `scip_pb2.py` is generated from `scip.proto` at sourcegraph/scip **v0.9.0** (regenerated from v0.7.0 because v0.7.0 lacked the `typed_range` oneof that `scip-swift` requires)
- **SQLite schema:** Output of `scip expt-convert` (not a published spec, treated as a moving target across releases)
- **Code standards:** Dataclasses over Pydantic, stdlib sqlite3 (no ORMs), broad exception-handling in MCP server (uniform error payload), atomic pointer-swap for publish safety

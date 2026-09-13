# jarvis

<!-- mcp-name: io.github.phuongddx/jarvis -->

[![CI](https://github.com/phuongddx/jarvis/actions/workflows/test.yml/badge.svg)](https://github.com/phuongddx/jarvis/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/jarvis-mcp.svg)](https://pypi.org/project/jarvis-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/jarvis-mcp.svg)](https://pypi.org/project/jarvis-mcp/)
[![Platforms](https://img.shields.io/badge/platform-macOS%20%20%2F%20%20Linux-lightgrey)](https://pypi.org/project/jarvis-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/jarvis-mcp.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-2e5aa8)](https://modelcontextprotocol.io)

**Your coding agent should not spend its context window on grep.**

When your agent asks *"where is `AuthService` used?"*, grep returns comments,
markdown, test fixtures, and similarly named symbols in one noisy pile. The
agent burns context re-reading files to filter signal from noise — and still
misses call sites.

jarvis precomputes a local code-intelligence layer so your agent calls
`findReferences` for exact occurrences, `goToDefinition` for the defining
range, `callHierarchy` for call paths, and `semanticSearch` for plain-language
questions — **ten MCP tools** for Claude Code, Cursor, or any MCP client.
Your code and indexes never leave your machine.

| | |
|---|---|
| **Without jarvis** | Grep → comments, tests, imports mixed with real hits → opens files to filter → guesses at call sites → burns context on search instead of reasoning |
| **With jarvis** | `findReferences("AuthService")` → exact file-and-range occurrences → `callHierarchy` for the graph → `semanticSearch("where is token refresh handled?")` → answers in milliseconds |

<p align="center">
  <img src="docs/assets/demo.gif" width="780" alt="grep returns noisy results vs jarvis findReferences returning 10 exact file:line:col occurrences">
</p>

[Quick start](#quick-start) · [MCP tools](#mcp-tools) · [Dashboard](#dashboard) · [Limits](#requirements-and-limits) · [Details](#details) · [Docs](#documentation)

## Quick start

**1. Install** (Tree-sitter syntax baseline ships in the wheel — no external binaries needed):

```bash
uv tool install jarvis-mcp
```

**2. Index a repo** (slug defaults to the directory name):

```bash
jarvis index /path/to/your/repo
```

**3. Register the MCP server:**

```bash
claude mcp add jarvis --scope user -- jarvis-server
```

Ask your agent *"find all references to `AuthService`"* — it calls
`findReferences` instead of grepping.

<details>
<summary>Other MCP clients (Cursor, Claude Desktop, any stdio client)</summary>

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "jarvis-server"
    }
  }
}
```

If your client can't find `jarvis-server` on `PATH` (GUI apps often don't
inherit your shell's), use the absolute path from `which jarvis-server`.
</details>

<details>
<summary>Claude Code plugin (registers MCP + ships agent skills)</summary>

```
/plugin marketplace add jarvis-intelligence/jarvis-index
/plugin install jarvis@jarvis
```
</details>

<details>
<summary>Full precise navigation — optional SCIP/Zoekt enrichment</summary>

For exact references, call hierarchy, and type hierarchy on TypeScript/TSX,
Python, Java/Kotlin, and Swift, install external indexer binaries:

```bash
curl -fsSL https://raw.githubusercontent.com/jarvis-intelligence/jarvis-index/main/setup.sh | sh
jarvis reindex /path/to/your/repo
```

Without them, `documentSymbols` and `goToDefinition` still work via the
Tree-sitter baseline (17 languages); `findReferences`, `callHierarchy`, and
`typeHierarchy` require SCIP data and return a capability error with recovery
guidance if it's missing.
</details>

<details>
<summary>Optional extras</summary>

```bash
uv tool install "jarvis-mcp[watch]"      # + watchdog, for `jarvis watch`
uv tool install "jarvis-mcp[semantic]"   # + lancedb/sentence-transformers, for semanticSearch
```
</details>

## MCP tools

| Tool | What it does |
|------|-------------|
| `goToDefinition` | Resolve a symbol to its defining file and range |
| `findReferences` | Every occurrence of a symbol across the indexed repo |
| `callHierarchy` | Incoming/outgoing calls for a symbol |
| `typeHierarchy` | Supertypes/subtypes |
| `documentSymbols` | Outline of every symbol defined in one file |
| `searchCode` | Zoekt lexical/regex search, optionally filtered to one repo |
| `semanticSearch` | Natural-language search fused with lexical + symbol hits |
| `blastRadius` | Which other indexed repos depend on a package, up to 2 hops |
| `getIndexStatus` | Published commit, freshness, staleness vs. working tree |
| `indexRepo` | Build an index for a git repo at `path` |

## Dashboard

```bash
jarvis dashboard          # serves http://127.0.0.1:6080 and opens a browser
```

A localhost web console over the same `~/.jarvis` data the CLI and MCP server read:

<p align="center">
  <img src="https://raw.githubusercontent.com/phuongddx/jarvis/main/docs/assets/dashboard-repos.png" alt="Repositories view" width="380">
  &nbsp;
  <img src="https://raw.githubusercontent.com/phuongddx/jarvis/main/docs/assets/dashboard-playground.png" alt="Tool playground" width="380">
</p>

## Requirements and limits

- **macOS and Linux only.** Windows is not supported.
- **One language per repo** — detected by extension plurality across git-tracked files; override with `--language`.
- **jarvis never edits code.** It is the retrieval half — [Serena](https://github.com/oraios/serena) complements it for renames/refactors.
- **Indexing is explicit** — run `jarvis index` (or `jarvis watch`) before querying.

## Details

<details>
<summary>How it works</summary>

One indexing CLI writes precomputed indexes into `~/.jarvis`; one stdio
runtime reads them down. The two share no other contract.

<p align="center">
  <img src="https://raw.githubusercontent.com/phuongddx/jarvis/main/docs/assets/jarvis-architecture.png" width="780" alt="jarvis architecture">
</p>

- **Publishing is atomic** — a reindex writes a new snapshot, then flips the `current` pointer via `os.replace`. A query reading the old snapshot keeps working; no downtime window.
- **The runtime path never writes** — every query opens the published snapshot read-only (`mode=ro&immutable=1`).
- **The package graph is rebuilt, not accumulated** — each reindex clears that repo's outgoing edges before recomputing.

Full detail: [`docs/system-architecture.md`](docs/system-architecture.md)
</details>

<details>
<summary>Indexing CLI reference</summary>

```bash
jarvis index /path/to/your/repo            # slug defaults to the directory name
jarvis index /path/to/your/repo --slug foo # or pick one explicitly
jarvis index /path/to/your/repo --scheme MyScheme # Swift repo with an ambiguous Xcode scheme
jarvis index /path/to/your/repo --language python # force the language instead of detecting it
jarvis index /path/to/your/repo --no-scip   # skip optional SCIP enrichment; syntax baseline + Zoekt still publish
jarvis index /path/to/your/repo --scip      # re-enable SCIP enrichment (both flags persist per repo)
jarvis list
jarvis status foo
jarvis reindex foo
jarvis forget foo
jarvis watch /path/to/your/repo [--debounce 5.0]  # debounced auto-reindex on file changes (foreground)
```
</details>

<details>
<summary>Known upstream limitations</summary>

- **`scip-java` can't index Android/Gradle repos** — its Gradle plugin keys off standard source sets that AGP replaces ([scip-java#177](https://github.com/scip-code/scip-java/issues/177)). Detected automatically; degrades to search-only.
- **Kotlin indexing requires an exact Kotlin version match** — `scip-kotlinc` is compiled against one pinned release (currently `2.2.0`). Detected automatically.
- **Maven-built Java repos need bash ≥ 4.4 on macOS** — `brew install bash` fixes it.
- **Swift indexing requires `scip >= v0.9.0`** — older converters silently drop occurrence ranges. `jarvis index` refuses an older `scip` rather than publishing a broken index.
</details>

<details>
<summary>Configuration</summary>

| Variable | Purpose |
|----------|---------|
| `JARVIS_DATA_DIR` | Override default `~/.jarvis` for all indexes and registry |
| `JARVIS_EMBEDDING_QUERY_PREFIX` / `JARVIS_EMBEDDING_DOC_PREFIX` | Override embedding instruction prefixes (auto-detected for bge-m3, e5, nomic-embed) |

`jarvis index --no-semantic` skips the vector stage even when the `semantic` extra is installed. The MCP `indexRepo` tool defaults to `--no-semantic` so an agent tool call never implicitly downloads embedding weights.
</details>

## Documentation

- [`docs/project-overview-pdr.md`](docs/project-overview-pdr.md) — scope, value prop, out-of-scope items
- [`docs/system-architecture.md`](docs/system-architecture.md) — architectural guarantees, storage layout, query paths
- [`docs/codebase-summary.md`](docs/codebase-summary.md) — module map, test coverage
- [`docs/code-standards.md`](docs/code-standards.md) — code patterns and conventions
- [`docs/dashboard.md`](docs/dashboard.md) — dashboard views, actions, security model

## License

[MIT](LICENSE)

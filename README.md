# jarvis

[![CI](https://github.com/phuongddx/jarvis/actions/workflows/test.yml/badge.svg)](https://github.com/phuongddx/jarvis/actions/workflows/test.yml)
[![Platforms](https://img.shields.io/badge/platform-macOS%20arm64%2Fx86__64%20%7C%20Linux%20arm64%2Fx86__64-lightgrey)](#requirements-and-limits)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-2e5aa8)](https://modelcontextprotocol.io)

**Your coding agent should not spend its context window on grep.**

When your agent asks *"where is `AuthService` used?"*, grep returns comments,
markdown, test fixtures, and similarly named symbols in one noisy pile. The
agent burns context re-reading files to filter signal from noise — and still
misses call sites.

jarvis precomputes a local code-intelligence layer so your agent calls
`findReferences` for exact occurrences, `goToDefinition` for the defining
range, `callHierarchy` for call paths, and `searchCode` for precise lexical
search — **ten MCP tools** for Claude Code, Cursor, or any MCP client.
Your code and indexes never leave your machine.

| | |
|---|---|
| **Without jarvis** | Grep → comments, tests, imports mixed with real hits → opens files to filter → guesses at call sites → burns context on search instead of reasoning |
| **With jarvis** | `findReferences("AuthService")` → exact file-and-range occurrences → `callHierarchy` for the graph → `searchCode("token refresh")` → answers without opening every match |

<p align="center">
  <img src="docs/assets/demo.gif" width="780" alt="grep returns noisy results vs jarvis findReferences returning 10 exact file:line:col occurrences">
</p>

[Quick start](#quick-start) · [MCP tools](#mcp-tools) · [Dashboard](#dashboard) · [Limits](#requirements-and-limits) · [Details](#details) · [Docs](#documentation)

## Quick start

**1. Install the standalone binary:**

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

Linux users need [Homebrew/Linuxbrew](https://docs.brew.sh/Homebrew-on-Linux)
installed first. The formula supports macOS arm64/x86_64 and Linux
arm64/x86_64, installs both `jarvis` and `jarvis-server`, and embeds Python
3.12, dashboard assets, tree-sitter libraries, `watch`, `scip`,
`zoekt-git-index`, and `zoekt-webserver`. Homebrew supplies
`universal-ctags`. No Python, uv, pip, or PyPI installation is required.

Upgrade with `brew upgrade jarvis`; recover a damaged installation with
`brew reinstall jarvis`.

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

Install the Homebrew binary first. The plugin adds jarvis skills and can
register the already-installed `jarvis-server`.

```
/plugin marketplace add jarvis-intelligence/jarvis-index
/plugin install jarvis@jarvis
```
</details>

<details>
<summary>Optional language indexers</summary>

The standalone package includes the SCIP converter and both Zoekt binaries.
The Homebrew formula does not install `setup.sh`; from a jarvis source
checkout, install the optional per-language indexer for your repo:

| Language | Installer | Toolchain |
|---|---|---|
| TypeScript/TSX | `sh setup.sh --only scip-typescript` | Node.js/npm |
| Python | `sh setup.sh --only scip-python` | Node.js/npm |
| Java/Kotlin | `sh setup.sh --only scip-java` | JDK; Kotlin repos require Kotlin 2.2.0 exactly |
| Swift | `sh setup.sh --only scip-swift` | Xcode on macOS arm64 |

For Maven-built Java repositories on macOS, also install a modern Bash with
`brew install bash`, then run `sh setup.sh --only bash-shim` from that same
source checkout. Reindex after adding an indexer. Without the matching
indexer, `documentSymbols` and `goToDefinition` still work via Tree-sitter
where coverage exists;
`findReferences`, `callHierarchy`, and `typeHierarchy` return recovery
guidance.
</details>

<details>
<summary>Not included</summary>

`semanticSearch` is registered for MCP compatibility but is excluded from the
standalone distribution. It returns a Homebrew-specific unavailability error;
lexical search and symbol search remain available.
</details>

<details>
<summary>Source-build semantic search</summary>

The standalone Homebrew package excludes semantic dependencies. In a source
checkout, install them with:

```bash
uv run jarvis install-semantic
```

Then index explicitly:

```bash
uv run jarvis index /path/to/repo --semantic
```

The first embedding run downloads the configured model.
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
| `semanticSearch` | Returns the Homebrew distribution's semantic-unavailability error |
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

- **macOS arm64/x86_64 and Linux arm64/x86_64 only.** Windows is not supported.
- **Semantic search is unavailable** in the Homebrew distribution.
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

The source-development semantic path has additional model-prefix overrides;
they have no effect in the standalone distribution.
</details>

## Documentation

- [`docs/project-overview-pdr.md`](docs/project-overview-pdr.md) — scope, value prop, out-of-scope items
- [`docs/system-architecture.md`](docs/system-architecture.md) — architectural guarantees, storage layout, query paths
- [`docs/codebase-summary.md`](docs/codebase-summary.md) — module map, test coverage
- [`docs/code-standards.md`](docs/code-standards.md) — code patterns and conventions
- [`docs/dashboard.md`](docs/dashboard.md) — dashboard views, actions, security model

## License

[MIT](LICENSE)

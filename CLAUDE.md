# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal, local-first code intelligence MCP server. SCIP-backed navigation (go-to-definition,
find-references, call/type hierarchy, document symbols) + Zoekt-backed lexical search, exposed
as MCP tools over stdio — no HTTP server, no auth, no network.

## Commands

```bash
uv sync                              # install deps
uv sync --extra watch                # + watchdog, needed for `jarvis watch`
uv sync --extra semantic             # + lancedb/sentence-transformers, needed for source-build semanticSearch

uv run pytest                        # all tests
uv run pytest -m "not integration"   # unit only — no external binaries required
uv run pytest -m integration         # integration only — runs real scip-python/scip/zoekt-git-index
uv run pytest tests/test_query.py::test_go_to_definition_returns_location   # single test

uv run jarvis index /path/to/repo [--slug name] [--scheme name] [--language name] [--semantic-include path]
uv run jarvis list
uv run jarvis status <slug>
uv run jarvis reindex <slug>
uv run jarvis forget <slug>
uv run jarvis watch /path/to/repo [--debounce 5] [--scheme name] [--language name] [--semantic-include path]   # foreground, not a daemon

uv run jarvis-server              # MCP stdio entry point
claude mcp add jarvis --scope user -- uv --directory /path/to/jarvis run jarvis-server
```

Unit tests need no external binaries. Integration tests expect the applicable language indexer,
`scip`, `zoekt-git-index`, and `zoekt-webserver` on `PATH`; they skip cleanly when absent.
From this source checkout, `setup.sh` installs only optional language indexers
(`--only scip-swift`, `scip-typescript`, `scip-python`, `scip-java`, or
`bash-shim`) — not the core binaries. The Homebrew package does not install
`setup.sh`.

## Architecture

Three engines sit behind the MCP server, each backed by its own storage:

- **Query** (`query.py` + `index_reader.py` + `scip_decoder.py`) — SCIP nav ops via raw SQL
  against the `scip expt-convert` SQLite schema (`documents`/`chunks`/`global_symbols`/`mentions`).
  `scip_decoder.py` is the *only* module importing `scip_pb2`/`zstandard` — an isolation seam so
  future SCIP proto version bumps localize to one file. `symbols.py` is the same kind of isolation
  seam for the SCIP *symbol string* grammar: it owns parsing (`parse_symbol()`) and bare-name
  resolution (`resolve()`, a two-rung ladder of exact match then dotted-suffix match) end to end, so
  `goToDefinition`/`findReferences`/`callHierarchy`/`typeHierarchy` accept a bare or qualified name
  instead of requiring the full, version-pinned SCIP string. Caveat: scip-swift emits clang USR
  strings (e.g. `c:@CM@UIKit@@objc(cs)UIView(im)centerXAnchor`) as its symbol names, so bare-name
  resolution has no practical value in Swift repos — it works for Python/TypeScript/Java/Kotlin.
- **Search** (`search.py`) — `searchCode` via a real `httpx` client to `zoekt-webserver`.
  `ZoektLifecycle` lazily spawns the webserver on first call (pidfile-tracked, killed at exit);
  never spawn it elsewhere. `base_url_if_running()` is the non-spawning variant, used by
  `getIndexStatus`'s coverage check so a status call never starts a server as a side effect.
- **Graph** (`graph.py` + `registry.py`) — package dependency graph (`packages`/`edges` in
  `registry.db`) driving `blastRadius` (2-hop BFS). `populate_graph_for_repo()` clears a repo's
  outgoing edges before recomputing — rebuild-not-accumulate, so retracted dependencies don't linger.
- **Semantic** (`chunker.py` + `embeddings.py` + `semantic.py` + `symbol_search.py`) — tree-sitter
  chunking → sentence-transformers embeddings → a per-repo LanceDB table under `~/.jarvis/lancedb/`.
  `semanticSearch` fuses vector hits with Zoekt hits and SCIP symbol-definition matches (via
  `symbol_search.py`, reusing `symbols.py`'s name-map machinery) via reciprocal rank fusion. Gated
  behind the optional `semantic` extra; the indexing stage is non-fatal in `jarvis index` (a failure
  there never blocks the SCIP/Zoekt publish). A LanceDB table only ever holds vectors from one
  model+revision — the model-identity rule. This path is source-build only: the standalone
  distribution excludes semantic dependencies and `semanticSearch` returns a Homebrew-specific
  unavailability error.

**Index pipeline** (`index_cli.py`, `index_repo()`): detect language by extension plurality
**across git-tracked files** (ties broken by fixed priority `.ts→.tsx→.py→.java→.kt→.swift`; one language per repo, no
multi-language merge) → run the matching indexer → `scip expt-convert` → populate the graph →
`zoekt-git-index` → **atomic publish**: write the new versioned `index-<sha>.db`, and only once graph +
Zoekt both succeed, flip the `current` pointer file via `os.replace()`. A query already reading the
old file is never interrupted; a failure anywhere leaves the previous index live. Never mutate a
published `index-<sha>.db` in place — queries always open it `mode=ro&immutable=1`.

**Search indexes git, not the filesystem — and indexes HEAD.** `zoekt-git-index` walks the git
tree and reads blobs by SHA, so gitignored content (`.venv/`, `node_modules/`, vendored
checkouts) is excluded by construction rather than by a denylist. This is the same
read-git-not-the-filesystem rule `detect_language()` follows, for the same reason: a filesystem
walk once made jarvis's own Zoekt index 241 MB / 7353 documents for a repo with 133 tracked
files. The trade-off is that **search reflects HEAD while SCIP navigation reflects the working
tree** — uncommitted edits are navigable but not searchable until committed.

`zoekt-git-index` has no `-meta` flag, so the Zoekt repository name is pinned with
`git config zoekt.name <slug>` (`_pin_zoekt_repo_name`); `forget` unsets it. Without the pin,
zoekt derives the name from the `origin` remote URL, url-escaped, and `searchCode`'s `r:<slug>`
filter silently matches nothing. `-shard_prefix_override` is not a substitute — it renames the
shard file only. Because that key is per-repo, **one slug per repo path** is enforced at index
time. Caveat: `git config` on a linked worktree writes to the repository's shared config, so
`zoekt.name` is not actually per-worktree — two slugs indexing two worktrees of the same repo
can race to set it. The one-slug-per-path check still prevents the common case (one slug, one
path); this is a narrow edge case that self-heals on the next index.

`<NNNNN>` in `<slug>_v16.<NNNNN>.zoekt` is a **shard ordinal, not a version** — a repo whose
corpus exceeds `-shard_limit` (100 MiB) is split across several shards, all current. Reindexing
overwrites shards in place and `zoekt-git-index` deletes its own surplus, so there is nothing to
garbage-collect; deleting all but the highest-numbered shard destroys most of a large repo's
index. `getIndexStatus`'s `searchCoverage` compares the `tracked_files` recorded at index time
against zoekt's live `Documents` precisely so that kind of loss is reported instead of silently
serving partial results.

**Distribution repos.** This repository is private development/source. End users install
`brew install jarvis-intelligence/jarvis/jarvis`; the generated formula lives in public
`jarvis-intelligence/homebrew-jarvis`. `publish-native.yml` builds four self-contained
archives (embedding Python 3.12, `watch`, dashboard assets, tree-sitter, `scip`,
`zoekt-git-index`, and `zoekt-webserver`), stages them to the tap's release, validates
the formula on four runner environments, and commits it. Homebrew supplies
`universal-ctags`; semantic dependencies are deliberately excluded. `build-zoekt.yml`
and `build-scip.yml` publish their pinned binary assets to public
`jarvis-intelligence/jarvis-index`; that repo also remains the independent source of
truth for Claude Code/Codex plugin content. Development uses uv; the release build's
Cython wheel is PyInstaller input and is not published as an installation artifact.

**Language detection reads git, not the filesystem:** `detect_language()` counts extensions across
`git ls-files`, not a `rglob` walk. A walk also counts gitignored scratch directories — vendored
checkouts, sibling clones, `.worktrees/` — which can outnumber a repo's own code and pick a
language it doesn't use. (Real case: a repo with 81 tracked `.py` files and a gitignored
`.local-checkouts/` of 4782 `.ts`/`.tsx` files was detected as TypeScript.) `IGNORED_DIRS` is still
applied on top, because git does not exclude build output a repo happens to commit. A non-git path
raises `NotAGitRepositoryError`. This is a heuristic with known edge cases (git shows duplicate
entries for unmerged paths, sparse-checkout entries absent from disk still count, and repositories
with code entirely in git submodules won't be counted). Pass `--language <name>` on the first
`jarvis index` to override detection for a polyglot repo — it's persisted in the registry, so
`reindex`/`watch` reuse it automatically.

**Single-tenant hardcoding:** `config.py` pins `PROJECT = "_"` and `BRANCH = "_"`. The on-disk
`scip/_/<slug>/_/` path shape is an artifact of reusing the vendored `IndexConnectionCache`'s
`(project, repo, branch)` 3-tuple key unchanged — not a real multi-tenancy feature. Only `<slug>`
is a real, user-facing identifier.

**Error handling at the MCP boundary:** `server.py` catches all exceptions per-tool and returns
`{"error": "..."}` rather than raising — this keeps the stdio server alive across query bugs. Every
nav tool takes `repo` (the slug from `jarvis index`) plus a tool-specific `symbol` or `path`.

**typeHierarchy needs the fork-built scip.** Upstream `scip expt-convert` (through v0.9.0) declares
`global_symbols.relationships` but never populates it — reported as
[scip#464](https://github.com/scip-code/scip/issues/464), fix PR
[scip#465](https://github.com/scip-code/scip/pull/465) still open. Native packaging therefore fetches a
build of the public fork `phuongddx/scip` (v0.9.0 + the fix), cross-compiled by `build-scip.yml`
from the commit pinned in `SCIP_COMMIT` and published to jarvis-index releases — the same pattern
as zoekt. Indexes created with an unpatched upstream binary still make `typeHierarchy` return an
explicit `{"error": ...}` — deliberately *not* empty arrays, which would wrongly assert "no
supertypes"; `relationship_data_present()` self-heals on reindex. Exit ramp: when upstream merges
#465 and releases, repoint native packaging at `scip-code/scip` releases and delete `build-scip.yml` + `SCIP_COMMIT`.

**Java/Kotlin reach is narrower than "supported" suggests.** `scip-java` indexes plain JVM
Gradle/Maven repos, and jarvis forces `-Dorg.gradle.parallel=false` for them because scip-java's
Gradle plugin races against itself across modules. Two cases cannot work at all and degrade to a
search-only publish instead:

- **Android/AGP** — scip-java's plugin keys off Gradle's standard `SourceSetContainer`, which AGP
  replaces with its variant model, so the build succeeds and emits zero SCIP shards
  ([scip-java#177](https://github.com/scip-code/scip-java/issues/177)).
- **Kotlin other than the pinned version** — `scip-kotlinc` is compiled against exactly one Kotlin
  release (`SCIP_JAVA_KOTLIN` in `setup.sh`, currently 2.2.0). Kotlin's compiler-plugin API is
  internal and unstable: 2.1.21 and 2.3.20 fail with `AbstractMethodError`, and even 2.2.20 fails
  with `NoSuchMethodError`. Java is unaffected — `scip-javac` uses javac's stable plugin API.

Both are detected from the indexer's own error output (`_SEARCH_ONLY_SIGNATURES` in
`index_cli.py`), never from parsing build files, and the decision is persisted so `reindex`/`watch`
skip the doomed build. `--search-only` requests the same publish up front: Zoekt and semantic search
work, navigation tools return an explanation. Only listed signatures trigger it — any other indexer
failure is still a hard failure.

**Maven Java repos need bash >= 4.4 on macOS.** scip-java generates a `javac`
wrapper (`#!/usr/bin/env bash`, `set -eu`) that expands `"${LAUNCHER_ARGS[@]}"`
unguarded. maven-compiler-plugin's version probe passes no `-J` flags, so the
array is empty — an error on bash < 4.4, and macOS ships only 3.2. Every
Maven-built Java repo fails at `default-compile` with
`LAUNCHER_ARGS[@]: unbound variable`.

Because the wrapper's shebang resolves bash through `PATH`, `setup.sh` creates
`~/.jarvis/shims/bash` pointing at a bash >= 4.4, and `_java_indexer_env()`
prepends that one directory for the indexer subprocess. Only the shim dir is
prepended, never a general bin dir — that would shadow `java`/`mvn`/`git` for
the build. Gradle is unaffected: it does not use the forked-javac wrapper.

Unlike the AGP case this is *fixable*, so it does not degrade to search-only —
`--search-only` cannot be un-set, which would strand a user who later installed
bash. It raises an error naming the remedy and leaves the repo `failed`.

**Swift build-tool selection:** `scip-swift`'s own `BuildBackendDetector` picks `swiftpm`
whenever `Package.swift` exists, even for repos that can't build that way (e.g. a UIKit-only
iOS package with no macOS platform support). `index_cli.py`'s `_prefers_xcodebuild()` overrides
this: a Swift repo with a checked-in `.xcodeproj`/`.xcworkspace` is indexed via `--build-tool
xcodebuild` instead. When such a repo has more than one scheme, pass `--scheme <name>` on the
first `jarvis index` — it's persisted in the registry, so `reindex`/`watch` reuse it
automatically.

## Testing conventions

Test files mirror source modules 1:1 (`test_query.py` ↔ `query.py`, etc.) — `models.py` and
`__init__.py` are the only modules without one. Unit tests mock subprocess/file I/O/HTTP; integration
tests (`@pytest.mark.integration`, concentrated in `test_index_cli.py`) call real binaries end-to-end
against fixtures in `tests/fixtures/`.

## Conventions worth knowing

- Result types are frozen dataclasses (`@dataclass(frozen=True)`), not Pydantic — there's no HTTP
  boundary to validate against, and `server.py` converts them via `dataclasses.asdict()`.
- Direct `sqlite3`, no ORM, always parameterized queries.
- Modern type-hint syntax throughout: `str | None`, `list[T]`, `dict[K, V]`.
- Env vars are prefixed `JARVIS_` (`JARVIS_DATA_DIR`, `JARVIS_EMBEDDING_MODEL`,
  `JARVIS_EMBEDDING_BATCH_SIZE`, `JARVIS_EMBEDDING_QUERY_PREFIX`,
  `JARVIS_EMBEDDING_DOC_PREFIX`, `JARVIS_ZOEKT_BIN`).

## Cutting a release

Use the `.claude/skills/jarvis-release` skill (project-scoped, maintainer-only — distinct from
the end-user plugin skills, which live in jarvis-index). The runbook bumps the sole version
source in `pyproject.toml`, refreshes `uv.lock`, runs tests and the version checker, merges
a release PR, signs the tag, publishes its GitHub Release, then requires all four native builds, all
four Homebrew validations, the public tap formula commit, and a local Homebrew install check
to pass. The Claude Code and Codex plugins version separately in jarvis-index.

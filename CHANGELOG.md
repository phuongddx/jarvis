# Changelog

## [0.11.0] - 2026-09-14

### Changed

- Replaced the uv/PyPI installation path with a Homebrew standalone
  distribution for macOS arm64/x86_64 and Linux arm64/x86_64.
- Bundled Python 3.12, `watch`, dashboard assets, tree-sitter libraries,
  pinned SCIP, Zoekt indexing, and Zoekt search binaries.
- Excluded optional semantic dependencies from the standalone distribution.
- Removed PyPI, MCP Registry, and legacy bootstrap publication workflows.

## [0.10.0] - 2026-09-12

Minor rather than patch: jarvis gains a localhost operator dashboard — the
registry, index runs, and all ten MCP tools become watchable and drivable
from a browser without anything leaving the machine.

### Added

- **`jarvis dashboard`, a localhost web console over `~/.jarvis`.** Four
  views: **Repos** (every registered repo's status and freshness with live
  index-log tails), **repo detail** (published snapshots and which one the
  `current` pointer selects, per-tool capabilities, recovery guidance,
  package-graph edges, storage footprint), **Search** (one query fanned to
  Zoekt lexical, semantic-vector, and SCIP-symbol results, with an
  in-browser source viewer), and a **Playground** that invokes any of the
  ten MCP tools with typed parameters and shows the raw response.
- **Operator actions reuse the `indexRepo` spawn seam.** Dashboard index and
  reindex call `server._spawn_index` — the same detached `jarvis index`
  children, write-once launch records, and per-slug build lock the MCP tool
  uses, so browser-initiated and agent-initiated runs arbitrate through one
  mechanism (a slug already in flight gets 409, never a queued second
  writer). **Forget** is the CLI's own `forget_repo` — build lock honored,
  refuses under a live writer — behind a typed-slug confirmation.
- **Zero new dependencies.** The server is stdlib `http.server`
  (`ThreadingHTTPServer`) behind a Host-header guard plus an Origin check on
  POSTs; it binds `127.0.0.1` only and is deliberately unauthenticated —
  single-tenant, the same contract as the MCP server. The frontend is three
  framework-free static files served via `importlib.resources`.
- **xAI-styled assets ship in the wheel.** `dashboard_assets/` (one HTML
  shell, one JS module, one stylesheet on the xAI token sheet) rides in
  every wheel via package-data, with `check_wheel_contents.py` guarding its
  presence at release time.
- **WAI-ARIA tab navigation.** `nav.tab-nav` is a real tablist — `role="tab"`
  per view, `aria-controls`/`aria-selected`, roving tabindex, and
  ArrowLeft/ArrowRight/Home/End activation — over an xAI-token visual
  contract (near-black surfaces, hairline borders, white primary pills,
  monospace instrument labels, a restrained sunset-orange accent), with
  `:focus-visible` outlines, dot-plus-label status cues (never color alone),
  and `prefers-reduced-motion` support.
- **Serialized semantic worker in the dashboard.** Concurrent first-use of
  the `semanticSearch` embedding stack (bge-m3/sentence-transformers) from
  the dashboard's HTTP threads segfaulted the process on macOS/arm64 — two
  model loads racing in one interpreter. All dashboard embedding traffic now
  runs through a single worker thread and callers wait at most 20s: a cold
  first load (model download) degrades that one signal to a "timed out"
  error while the load finishes, and later calls hit the warm model. A
  child of this fix also hardened `jsonNode` against the `null` fields real
  `documentSymbols` payloads contain, which crashed response rendering.

See `docs/superpowers/specs/2026-09-12-dashboard-design.md` and
`docs/superpowers/plans/2026-09-12-dashboard.md` for the design and
implementation plan.

## [0.9.1] - 2026-09-11

### Fixed

- **`jarvis index` crashed natively in the tree-sitter syntax baseline
  (jarvis-index#14).** The tree-sitter 0.26.0 runtime's binding refactor
  corrupts the heap non-deterministically during the declaration walk:
  SIGSEGV/SIGBUS at mobile crash sites (attribute read, GC pass, point
  construction), allocation-layout dependent — one added print line in the
  walk loop makes the same input survive, which is why 0.9.0's wheel-build
  test gates never saw it. Measured at 12 runs per cell on the same input:
  8/12 and 11/12 native crashes under 0.26.0 with either grammar version,
  0/12 under 0.25.2 with identical grammars — the runtime, not the grammar
  pairing, is the broken side. GuardMalloc traps no out-of-bounds write and
  isolated per-API probes run clean, so only real-sized sibling-frame walks
  trigger it; the exact C-level mechanism is unconfirmed (it would need an
  ASAN build of the binding) and is documented as such in the issue. Fix:
  pin `tree-sitter==0.25.2` with all 16 grammar pins unchanged, plus a
  subprocess-isolated regression test that walks a synthetic
  300-definition module in five fresh interpreters and fails on any
  nonzero child exit (12/12 native crashes under 0.26.0, 0/12 under
  0.25.2). Verified against the compiled release wheel (the original
  crasher reproduces 0/12 post-fix) and the full wheel-only resolution
  matrix (12/12 cp312–314 × darwin/linux cells resolve 0.25.2, no source
  builds).

## [0.9.0] - 2026-09-10

Minor rather than patch: jarvis gains its tenth MCP tool — an agent can now
bootstrap a missing index itself instead of receiving prose only a human can
act on — plus the run-coordination machinery that makes concurrent index
writers safe.

### Added

- **`indexRepo` MCP tool.** Called with a local git repo `path`, it runs
  pre-flight checks (jarvis binary resolution, Zoekt availability, git
  work-tree check, slug resolution against the registry), then spawns
  `jarvis index` as a detached child and returns immediately with
  `{slug, state, pid, log}`. Ordering is load-bearing: the launch record is
  written to disk *before* `Popen` (write-once — tempfile + `os.replace`),
  so an immediate poll can never see "nothing running"; the child's stdout
  is `DEVNULL` because one inherited write would corrupt the live JSON-RPC
  stream; `--slug` is always explicit because registry lookup may return a
  custom slug the child would never derive from the basename. `semantic`
  defaults to false — an agent tool call must never implicitly download
  embedding weights (~2 GB).
- **`getIndexStatus` reports an `indexing` block with a terminating poll
  contract.** Observation reaps the child first (an exited-but-unreaped
  child is a zombie whose pid still answers `os.kill(pid, 0)`, so pid
  liveness is useless — reaping yields the true exit code and
  `failed-at-startup`), then lock-held → `running`, then the registry row
  and launch record are correlated by timestamp (a record newer than the
  row's `last_indexed` is the *current* attempt, so a stale `failed` row
  can't end a fresh run's poll and a stale `indexing` row can't mask a dead
  one), and a record older than a startup grace window with nothing running
  → `abandoned`. Every observation is terminal or progressing — the loop
  always exits.
- **`src/jarvis/jobs.py`** owns that machinery: a per-slug `flock` build
  lock (fail-fast `LOCK_NB`; the lock file is never unlinked — unlinking
  lets a waiter flock a detached inode while a third process locks a fresh
  one), the write-once launch record, and the state derivation. Shared by
  the CLI writer and the MCP reader; no shared mutable handle exists.
- **`--no-semantic` / `--semantic` CLI pair and `index_repo(semantic=)`.**
  A tri-state like `--scip`: omitted means "stage runs when the extra is
  installed". Deliberately *not* persisted — it is a per-run cost decision,
  not a repo property.
- **Agent-actionable `IndexNotFoundError` payloads** now carry
  `recoveryTool: "indexRepo"` + `recoveryToolArgs` whether or not a registry
  row exists — the row-less case (a never-indexed repo) is exactly the one
  an agent hits first, and previously got bare `{"error": ...}`.

### Fixed

- **Two repos with the same basename silently overwrote each other's
  index.** `/a/app` and `/b/app` both derive slug `app`, and the second
  `upsert` overwrote the first's registration and artifacts. A derived slug
  already bound to a *different still-existing* path is now rejected,
  naming both paths; a moved repo (old path gone) is still allowed and
  re-paths the row. The guard runs inside the build lock, so the check and
  the write can't interleave.
- **Concurrent index runs on one slug raced.** Nothing arbitrated two
  writers: registry rows last-write-won and artifact writes interleaved.
  `index_repo` now holds the build lock from before the duplicate guards
  through the final publish; a loser gets `error: another index is already
  running for '<slug>'` (rc 1), and watch runs that lose the race retry on
  the next debounce event.
- **`jarvis forget` could destroy state under a live writer.** Registry
  row, graph edges, and artifacts were deleted with no coordination. Forget
  now takes the build lock around its entire teardown and refuses with rc 1
  while a writer holds it; the launch record and log are cleaned up as part
  of forgetting.

See `docs/superpowers/specs/2026-09-10-server-side-auto-index-design.md`
and `docs/superpowers/plans/2026-09-10-server-side-auto-index.md` for the
design and implementation plan.

## [0.8.1] - 2026-09-10

No functional changes — the repository moved from
`jarvis-intelligence/jarvis` to `phuongddx/jarvis` (the old path
301-redirects; every existing clone, badge, and link keeps working), and the
distribution identity follows it:

### Changed

- **MCP Registry listing is now `io.github.phuongddx/jarvis`.** The registry
  derives its namespace from the publishing workflow's GitHub OIDC identity,
  so a repo transfer forces a namespace change — the workflow could no longer
  publish under the old org's namespace regardless of what `server.json`
  declared. The `server.json` name, the README ownership marker, and the
  `publish-pypi.yml` preflight literal that asserts it move together (they
  must agree or the next release fails before upload). This release is the
  first published under the new namespace; the old
  `io.github.jarvis-intelligence/jarvis` entry is superseded and slated for
  removal from the registry.
- **PyPI trusted publishing re-pointed** to owner `phuongddx`, repo `jarvis`
  (workflow `publish-pypi.yml`, empty environment, unchanged). The PyPI
  package name `jarvis-mcp`, install commands, and all download URLs are
  unaffected.

### Fixed

- **Living documentation and release tooling now reference the new repo
  path** — README badge/clone/architecture-image URLs, the release runbook's
  `gh --repo` commands and marker check, and the three docs files naming the
  registry id. `jarvis-intelligence/jarvis-index` references are untouched by
  design: that repo stays in the org and remains the distribution target for
  `setup.sh` downloads, binary releases, and the plugin.

## [0.8.0] - 2026-09-10
Minor rather than patch: jarvis gains a build-free syntax baseline — every
`jarvis index`/`reindex`/`watch` run now publishes declaration-level
navigation for 17 languages without any external indexer — and the one-way
`--search-only` mode is replaced by reversible, persisted SCIP controls.

### Added

- **Tree-sitter syntax baseline, always on (spec TSI-01..TSI-04).**
  `jarvis index` captures every git-tracked source file, extracts named
  declarations with curated Tree-sitter grammars, and publishes them into the
  same immutable SQLite snapshot as the optional SCIP tables — selected by the
  single `current` pointer, now named `index-<sha>-<generation>.db` (fresh
  `uuid4().hex` generation per publish, so a same-commit reindex never touches
  a live filename). Incremental reuse keys rows on file hash + grammar
  identity, so unchanged files carry their rows forward and a grammar bump
  forces re-extraction. The 17 supported languages (Python, JavaScript,
  TypeScript/TSX, Java, Kotlin, Swift, Go, Ruby, Rust, C, C++, C#, PHP,
  Scala, Bash, SQL) ship as **base pip dependencies** — 16 pinned grammar
  distributions plus the `tree-sitter` runtime, resolved from prebuilt abi3
  wheels on every supported platform (verified by a 12-cell wheel-only
  resolution matrix over CPython 3.12/3.13/3.14 × macOS/Linux arm64/x86-64).
  Nothing is vendored into jarvis's wheel and nothing is downloaded at index
  time; a fresh `uv sync` with no extras parses all 17 languages offline.
- **Per-file provider routing on `documentSymbols`/`goToDefinition` (spec
  TSI-05).** A file with usable SCIP coverage is served by its SCIP
  outline/definitions; a file without it is served by real syntax
  declarations. Every location now carries `source` (`"scip"` or
  `"tree-sitter"`) and `positionEncoding`; syntax outline entries add
  `selectionRange`/`qualifiedName`/`parentSymbol`, `documentSymbols` adds a
  `coverage` object for syntax-served files, and bare/qualified names search
  both providers and merge candidates. Opaque `syntax:` identifiers returned
  by the baseline round-trip through `goToDefinition`. The SCIP-only tools —
  `findReferences`, `callHierarchy`, `typeHierarchy` — never fake results:
  without usable SCIP data they return the established error shape plus
  `requiredCapability`/`reason`/`recovery` instead of an empty array.
- **Live capabilities on `getIndexStatus`.** `capabilities.tools` reports, for
  each of the five navigation tools, whether its providers have data in the
  published snapshot (with reason/recovery when not); `capabilities.syntax`
  reports per-state extraction counts and the extraction identity; freshness
  names the snapshot `generation` so a caller can tell which immutable
  snapshot answered.

### Changed

- **`--search-only` is gone; `--scip`/`--no-scip` replace it (spec TSI-07).**
  The old opt-in degradation configuration was one-way: a persisted
  search-only choice could never be un-set. SCIP enrichment is now controlled
  by a reversible choice persisted per repo — `--scip`/`--no-scip` on `index`,
  `reindex`, and `watch`; omitted means use the persisted choice, defaulting
  to enabled for a new repo. SCIP tooling is no longer validated up front: a
  missing or failing indexer degrades the run to exit-0 `degraded` with the
  cause recorded, and the syntax baseline publishes regardless. The removed
  `--search-only`/`--fallback-search-only`/`--no-fallback-search-only` flags
  are rejected with their replacement (never silently mapped), and
  `JARVIS_FALLBACK_SEARCH_ONLY` is no longer read (a one-line note replaces a
  silent ignore). Watch runs whose SCIP attempt already failed at the current
  commit skip only that retry — the baseline still publishes; a new commit, an
  explicit reindex, or an explicit `--scip` retries enrichment.
- **Registry schema carries per-stage truth (spec TSI-06/TSI-08).** Rows gain
  `scip_enabled`/`scip_state` (available/partial/failed/unavailable/
  unsupported/disabled, read-normalized to `unknown` for legacy rows) plus
  failure evidence and `scip_failed_at_sha`; the overall status vocabulary is
  now `indexing`/`indexed`/`partial`/`degraded`/`failed`. A one-time,
  transactional, idempotent migration preserves every legacy row's facts
  (including historical search-only opt-outs, which map to `degraded` with an
  explanatory note) before the superseded columns are dropped. `forget` now
  also tears down the repo's graph edges and unpins its Zoekt repository name.
- **Shared parse input across the syntax and semantic stages (spec TSI-09).**
  `semantic.py`'s `index_semantic()` is split into `prepare_semantic()`/
  `finish_semantic()` so the chunker consumes the syntax stage's parse tree
  instead of reparsing; `chunk_file()` accepts a supplied `tree`, and its
  slicing is byte-safe Unicode (chunk `CONTENT_FORMAT` bumped to 2, so old
  semantic tables are fully re-embedded rather than mixed).

### Fixed

- **A semantic chunk-capture failure no longer poisons the publish.** The
  shared-parse cutover (TSI-09) moved chunk extraction into the capture step
  that runs before the atomic pointer flip, so a chunker exception on one
  file previously aborted the entire indexing run. Capture failures are now
  isolated to the optional semantic stage: the first error is recorded,
  collection stops, and the semantic finish step is never attempted — so the
  previous LanceDB table stays live while the new syntax snapshot publishes,
  with one warning to stderr.
- **`README.md` now matches the shipped behavior**: requirements describe the
  pip-installed grammars and per-language coverage, the tool table documents
  `source`/`coverage` provenance and the SCIP-required contract, and every
  reference to the removed search-only flags is replaced with the reversible
  controls and the degraded-status semantics.

## [0.7.2] - 2026-09-08

No functional changes — v0.7.1's own publish run also failed the
compiled-wheel build, for two further reasons the v0.7.1 fix missed. Same
code as v0.7.0/v0.7.1, ships for real this time.

### Fixed

- **One more `interactive_input` test was missed.**
  `test_cmd_index_semantic_include_runs_on_declined_repo_without_clearing_bit`
  also calls `_force_offer_seams`/`_offer_run` (the same real-`input()` path
  v0.7.1 marked seven other tests for) but its name doesn't match the
  `tty_offer`/`offer_` pattern the first pass was grepped against. Now
  marked and excluded from the cibuildwheel test-command like the other
  seven.
- **A real Cython compilation bug in `jarvis.symbols._matches`, exposed for
  the first time by this release's compiled-wheel test run.**
  `_matches`'s `name_map` parameter is annotated `dict[str, list[Candidate]]`,
  but its lookup used `name_map.get(key, ())` — an empty *tuple* default
  where the annotation promises `list` values. Pure Python's duck typing
  never noticed (iterating an empty tuple or empty list is identical), so
  this shipped unnoticed in every previous release. Cython's compiled code
  enforces the annotated value type on `dict.get()`'s default argument,
  raising `TypeError: Expected list, got tuple` on every call that missed
  the fast-path bucket — which broke `resolve()` for any unknown or
  dotted-suffix symbol query, cascading into ten failing tests
  (`goToDefinition`/`findReferences`/`callHierarchy` on unknown symbols, and
  every dotted-suffix resolution path). Fixed by defaulting to `[]`,
  matching the parameter's own declared type — a genuine latent bug, not a
  workaround, that only a real compiled-wheel test run could have caught.

## [0.7.1] - 2026-09-08

No functional changes — v0.7.0's first publish run never reached PyPI, so
this release exists solely to fix the compiled-wheel build and ship the
same code.

### Fixed

- **`publish-pypi.yml`'s compiled-wheel matrix failed on every platform,
  before any upload happened.** The `build-wheels` job runs the unit suite
  inside cibuildwheel's isolated `--test-command` subprocess as its
  Cython-fidelity gate; seven tests exercising the semantic-index install
  prompt (`test_cmd_index_tty_offer_*`, `test_cmd_index_offer_*`) monkeypatch
  `builtins.input` to script answers, but that subprocess does not give
  pytest a real, monkeypatch-honoring stdin the way the regular unit-test
  job's interpreter does — the replacement lost the race to pytest's own
  capture-mode stdin guard, raising `OSError: pytest: reading from stdin
  while output is captured!` on the very first affected test, and
  `fail-fast: true` then cancelled the other three matrix legs before any
  wheel was built or uploaded. These seven tests are unrelated to
  compilation itself — the regular unit job (`test.yml`) already runs them
  successfully on macOS and Linux on every push — so they are now marked
  `interactive_input` and excluded from the cibuildwheel test-command
  specifically, the same narrow, environment-scoped treatment
  `test_setup_sh.py` already gets there for its own unrelated (`dash`)
  environment mismatch. No test coverage is lost: the regular unit gate
  still runs all seven on every push and PR.

## [0.7.0] - 2026-09-08

Minor rather than patch: zoekt's `sym:` symbol search works for the first
time (jarvis never installed the `universal-ctags` binary zoekt auto-discovers
at index time, so every shard silently built with no symbol data), and
`searchCode`'s response shape changes — the old `total` field (always just
`len(hits)`, never a real total) is replaced by fields sourced from zoekt's
own match statistics.

### Added

- **`setup.sh` installs `universal-ctags`, reviving `sym:` symbol search and
  zoekt's symbol-definition ranking for every language.** zoekt only
  extracts symbols when `universal-ctags` (or `$CTAGS_COMMAND`) is on PATH
  at index time; jarvis never installed it, so every published shard had no
  symbol data and `sym:` queries silently returned nothing. Installing it
  wasn't enough on its own: Homebrew's and Debian's `universal-ctags`
  packages both install a binary literally named `ctags`, never
  `universal-ctags`, and zoekt's own detection does an exact-name PATH
  lookup for `universal-ctags` — so `setup.sh` now symlinks the real
  `ctags` binary under the name zoekt actually looks for. Surfaced as
  `capabilities.search.ctagsInstalled` in `getIndexStatus`. **To activate:**
  re-run `setup.sh`, then `jarvis reindex <slug>` — existing shards built
  without ctags stay symbol-less until reindexed.
- **`searchCode` reports true match totals.** `totalMatches` and `fileCount`
  come from zoekt's own `Result.Stats` (accumulated before display
  truncation, not from the returned page), `truncated` says whether more
  matches exist than were returned, and `indexedAt` reports which published
  snapshot the answer came from.
- **`JARVIS_ZOEKT_PORT`** overrides the embedded zoekt-webserver's port
  (previously hardcoded to `6070`).

### Fixed

- **`searchCode` and `semanticSearch` could return unbounded payloads.**
  zoekt's JSON search API applies no display or shard-match caps at all
  when the request omits `Opts` — a broad query could stream every match
  zoekt found (up to its own internal limits) into a single MCP response.
  Requests now send `Opts.MaxDocDisplayCount`/`MaxMatchDisplayCount`, and a
  single oversized match line (minified or generated files can put an
  entire file on one "line") is capped at 2000 characters with a visible
  truncation marker — previously such a line could be megabytes long.
- **The embedded zoekt-webserver's health check could adopt an unrelated
  process.** It accepted any HTTP response under 500 from whatever was
  listening on the zoekt port; it now requires zoekt's own `GET /healthz`,
  which only returns 200 after a real canary search succeeds against
  loaded shards. Spawn failures now capture the child's stderr so a bind
  conflict or crash is diagnosable instead of an opaque "exited
  immediately", and two jarvis processes racing to spawn the webserver now
  have the loser adopt the winner's server instead of erroring.
- **`semanticSearch`'s lexical signal was effectively dead on
  natural-language queries.** zoekt's default query conjunction is implicit
  AND, so a query like "how does the retry loop back off" ANDed six words
  together and typically matched nothing — the zoekt signal contributed
  nothing on exactly the queries `semanticSearch` exists for.
  Natural-language-shaped queries now OR-expand through the existing
  symbol-search tokenizer before reaching zoekt; code-shaped queries
  (identifiers, `sym:`/`lang:` syntax, quoted phrases) pass through
  unchanged.

### Changed

- **`searchCode`'s response shape.** The `total` field is removed; use
  `totalMatches` (true total), `returned` (length of `hits`), `truncated`,
  `fileCount`, and `indexedAt` instead.

## [0.6.2] - 2026-08-08

Pure bug fix: Swift indexing was unreachable through the installer for every
user. No change to any MCP tool signature or response shape.

### Fixed

- **`setup.sh --only scip-swift` 404'd on every macOS arm64 host.** The Swift
  indexer's repo moved off the personal `phuongddx` owner to the
  jarvis-intelligence org, and GitHub serves *no* redirect for the old path —
  so the pinned download URL returned 404 rather than forwarding, and
  `install_scip_swift` failed on every run. `SCIP_SWIFT_REPO` now points at
  `jarvis-intelligence/scip-swift`. Repointing alone was not sufficient: the
  move also dropped every tag and release asset from the repo, so v0.1.2 was
  republished from the same source (only `ci.yml` differs from the original
  tag; the binary still reports `0.1.2`). The asset checksum differs from the
  deleted release because it is a fresh build — `setup.sh` verifies against
  the `.sha256` sidecar published beside it, and existing installs are
  presence-gated, so nothing downstream needed changing.

  Two guards close the gap that let this ship silently. A test asserts
  `SCIP_SWIFT_REPO`'s owner never drifts back — the same drift assertion
  `ZOEKT_RELEASE_REPO` and `SCIP_RELEASE_REPO` already carried, which
  `SCIP_SWIFT_REPO` simply never had. And `setup-smoke.yml` now runs
  `--only scip-swift` on both runners: macOS arm64 downloads and executes the
  binary for real, Linux exercises the not-available skip branch (which must
  still exit 0). Previously no test read the variable and the smoke workflow
  only ever installed `scip` and `zoekt`, so the scip-swift download path had
  zero coverage anywhere.

## [0.6.1] - 2026-08-07

No functional changes — this release exists to move the project's publishing
identity to the jarvis-intelligence org after the repo transfer.

### Changed

- **MCP Registry entry renamed to `io.github.jarvis-intelligence/jarvis`.**
  The registry namespace is bound to the repo owner via GitHub OIDC, so after
  the transfer the workflow could no longer publish updates under
  `io.github.phuongddx/jarvis` — that old entry is orphaned at 0.6.0 and this
  release creates the successor. `server.json`, the README ownership marker,
  and publish-pypi.yml's marker guard changed in lockstep.
- **PyPI trusted publisher re-anchored** to owner `jarvis-intelligence`, with
  no environment: GitHub environments are unavailable on private repos under
  free-plan orgs, so the `pypi` environment (and its runbook) was dropped
  from publish-pypi.yml.
- **PyPI project URLs** now point at the public distribution repo
  `jarvis-intelligence/jarvis-index` (they referenced the pre-migration
  `phuongddx/jarvis-dist` name, which only worked via GitHub redirects).

## [0.6.0] - 2026-08-07

Minor rather than patch: `typeHierarchy` works for the first time, and the
packaging model changes from readable pure-Python wheels to Cython-compiled
platform wheels. No breaking change to any MCP tool signature or response
shape.

### Added

- **`typeHierarchy` now returns real super/subtypes** (#29). Upstream
  `scip expt-convert` (through v0.9.0) declares `global_symbols.relationships`
  in its schema but never writes it (scip-code/scip#464), so the tool
  returned an explicit error on every index. The fix (scip-code/scip#465) is
  still unmerged upstream, so `setup.sh` now installs a build of the public
  fork `phuongddx/scip` carrying it: `build-scip.yml` cross-compiles the fork
  at the commit pinned in `SCIP_COMMIT` and publishes the binaries to
  jarvis-index releases — the same pattern zoekt already uses. **To activate:
  re-run setup.sh, then `jarvis reindex <slug>`** — the scip install is
  version-gated (an installed binary that doesn't stamp the pinned commit is
  replaced exactly once per pin bump), and indexes built with an unpatched
  scip keep returning the explicit error until reindexed.

### Changed

- **Releases ship Cython-compiled wheels; source is no longer readable on
  PyPI** (#28). A pure-Python wheel is a zip of readable `.py` files, so
  repo privacy protected the development process but not the source. The
  build backend is now setuptools + Cython, gated by `JARVIS_COMPILE=1` (set
  only in release CI — local dev and editable installs stay pure Python):
  every module compiles to a native `.so` except `__init__.py` and the
  generated `scip_pb2.py`. Wheels cover cp312–cp314 on
  {linux x86_64/aarch64, macOS arm64/x86_64}; **no sdist is published**, so
  platforms outside that matrix fail loudly instead of falling back to
  readable source. Consequences: wheels ≤ 0.5.1 remain readable on PyPI
  forever; user-reported tracebacks now show compiled frames; musl/Alpine
  and Windows are not installable targets.

No functional changes relative to 0.1.0 — this release exists purely to fix
version resolution on PyPI.

### Changed

- **Version fast-forwarded past the orphaned pre-reset `0.5.0`.** The 0.0.1
  clean-slate reset deleted the pre-reset tags and GitHub Releases, but
  `jarvis-mcp 0.5.0` was never yanked on PyPI and remained the highest
  non-yanked version there. Every unpinned install — `uvx --from jarvis-mcp`,
  `pip install jarvis-mcp`, and the plugin's `--from "jarvis-mcp>=0.0.1"`
  floor — therefore resolved to the stale pre-reset 0.5.0 instead of 0.0.1
  or 0.1.0. Jumping to 0.5.1 makes the current code the effective latest for
  all resolvers without requiring a yank. The intended post-reset numbering
  (0.0.x/0.1.x) is abandoned; versioning continues from 0.5.1.

## [0.1.0] - 2026-08-06

Minor rather than patch: `semanticSearch` gains a new capability — a third
retrieval signal — and the `symbols` module grows a public accessor surface.
No breaking change to any MCP tool signature or response shape.

### Added

- **SCIP symbol-definition signal in `semanticSearch`** (#24). Previously the
  tool fused two signals — LanceDB vector hits and Zoekt lexical hits — via
  reciprocal rank fusion, and neither knows what a *definition* is: a query
  naming an identifier ranked chunks that merely mention it on par with the
  definition site. A new `symbol_search` module now turns the query into
  ranked definition locations (token extraction with stopword filtering,
  adjacent-token bigram concatenation for identifiers written as separate
  words, dotted-suffix matching; ranking by matched-token count, then kind
  priority TYPE > METHOD > TERM, then shorter dotted path) by matching
  against the SCIP name map and resolving through `defn_enclosing_ranges`.
  The signal enters the existing RRF unweighted, and merges into a vector
  chunk when that chunk contains the definition line. `sources` on a result
  may now include `"symbol"`; a symbol-only hit carries `content: ""` (the
  SCIP db stores no source text) with `symbolName` set to the definition's
  dotted path.
- Public `symbols.name_map()` and `symbols.dotted_suffix_matches()` accessors
  — the latter generalizes the existing rung-2 matching rule with a
  `case_sensitive` flag (default preserves `resolve()`'s exact behavior).

### Fixed

- SCIP's `defn_enclosing_ranges` stores 0-based line numbers while chunker
  and Zoekt coordinates are 1-based; the symbol signal now converts at the
  `SymbolHit` seam. Without the conversion, a definition's symbol hit missed
  its own chunk's containment check by exactly one line — producing duplicate
  content-less results and off-by-one `startLine`/`endLine` — because
  def-derived chunks start precisely on the definition line.

### Notes

- The signal is strictly additive and best-effort: repos published
  `--search-only`, `partial` indexes, or any failure inside the signal
  degrade to the previous two-signal result, byte-identical.
- Swift repos gain nothing from this signal: scip-swift emits clang USR
  strings as symbol names, which natural-language tokens never match — the
  same caveat that already applies to bare-name resolution in the nav tools.

## [0.0.1] - 2026-08-05

Initial clean-slate release of `jarvis-mcp` after the repository was reset to a
single commit. This version exists to re-establish the release pipeline (PyPI,
MCP Registry, Claude Code plugin, Codex plugin) from a known-good baseline with
no prior history.

No code changes relative to the pre-reset state — every source file, test, and
piece of documentation is byte-identical to what shipped before. The version
number is intentionally reset to `0.0.1` so the release artifacts published from
this commit do not collide with the orphaned pre-reset tags (`v0.2.0`–`v0.5.0`,
now deleted from the repository and from GitHub Releases).

### Distribution

- PyPI: `jarvis-mcp` 0.0.1 published via trusted publishing.
- MCP Registry: `io.github.phuongddx/jarvis` 0.0.1.
- Claude Code plugin: `jarvis` 0.0.1 from `phuongddx/jarvis-dist`.
- Codex plugin: `jarvis` 0.0.1.


## 0.5.0

Renamed the project from `codeintel` to `jarvis`. This is a breaking rename with
no automatic migration path.

**What you must do**

- Reinstall: the PyPI distribution is now `jarvis-mcp` (was
  `codeintel-navigation-mcp`), and the CLIs are `jarvis` and `jarvis-server`
  (were `codeintel` and `codeintel-server`).
- Re-add the plugin: it is now `jarvis`, served from
  `phuongddx/jarvis-dist` (was `phuongddx/jarvis`).
- Re-register the MCP server: `claude mcp remove codeintel` then
  `claude mcp add jarvis --scope user -- uv --directory /path/to/jarvis run jarvis-server`.
- Re-index your repos. The data directory moved from `~/.codeintel` to
  `~/.jarvis` and starts empty; nothing is migrated. The old tree is left
  untouched, so `mv ~/.codeintel ~/.jarvis` recovers existing indexes if you
  prefer — published index files carry no absolute paths — but that is a manual
  step, not a supported code path.
- Rename any `CODEINTEL_*` environment variables to `JARVIS_*`. The old names
  are ignored, not honoured, so a stale `CODEINTEL_DATA_DIR` in a shell profile
  fails loudly rather than silently pointing at the abandoned tree.

**Distribution**

- `codeintel-navigation-mcp` has been deleted from PyPI. Nothing resolves it any
  more, so an existing pin fails at install time rather than quietly serving a
  stale version — switch to `jarvis-mcp`.
- The MCP Registry entry is now `io.github.phuongddx/jarvis`. The five older
  `io.github.phuongddx/codeintel` entries (0.2.1 through 0.4.0) remain
  published, but each points at `codeintel-navigation-mcp` and therefore no
  longer resolves to an installable package.

All notable changes to this project are documented in this file.

## [0.4.0] - 2026-08-04

Minor rather than patch: `getIndexStatus` gains a new field
(`searchCoverage`) that detects a class of failure the previous release
could not see at all, alongside the fix that caused it.

### Fixed

- `zoekt-index` walked the filesystem, not the git tree, so it indexed every
  gitignored path — `.venv/`, `node_modules/`, vendored checkouts. On real
  repos this inflated one index from 133 tracked files to 7353 documents /
  241 MB. Zoekt splits an oversized index into numbered shard files
  (`<slug>_v16.<NNNNN>.zoekt`); `<NNNNN>` is a shard ordinal, not a version,
  but that bloat made it look like accumulated stale versions. Deleting "old"
  shards on that mistaken premise destroyed 15 of 16 shards of a real
  repository's index, and `searchCode` kept answering queries afterward with
  no error, silently missing most of the repo's content.

  Indexing now runs through `zoekt-git-index`, which reads blobs directly out
  of the git tree, so gitignored content is excluded by construction with no
  denylist to maintain. This does mean `searchCode` now reflects git HEAD,
  not the working tree — an uncommitted edit or new untracked file is
  findable via `grep` but not `searchCode` until it's committed; SCIP
  navigation is unaffected and still reflects the working tree.

  `zoekt-git-index` has no `-meta` flag, so the per-repo search index name is
  now pinned via `git config zoekt.name <slug>` instead; without it, Zoekt
  falls back to naming the index after the `origin` remote URL, and
  `searchCode(repo=<slug>)`'s `r:<slug>` filter would silently match nothing.
  Because that key is one value per repo, `jarvis index` now refuses a
  second slug for an already-indexed repo path, naming the conflicting slug
  and the `jarvis forget` remedy.

### Added

- `getIndexStatus` reports `searchCoverage: {expected, indexed, complete}` —
  the count of git-tracked files at last index time compared against what
  Zoekt's live index actually holds for that repo. This is the check that
  would have caught the incident above: a search index missing shards after
  a successful publish now reports `complete: false` instead of silently
  answering with partial results. When it can't be computed (e.g.
  `zoekt-webserver` isn't running, or the repo predates this field),
  `searchCoverage` is `null` with a `searchCoverageReason` explaining why.

## [0.3.2] - 2026-08-04

### Added

- Bare-name symbol resolution for the SCIP navigation tools. `goToDefinition`,
  `findReferences`, `callHierarchy`, and `typeHierarchy` now accept a bare symbol
  name (e.g. `build_mcp_server`) in addition to the existing dotted SCIP
  identifier, resolving it against the index automatically. Callers no longer need
  to construct the full SCIP symbol string (`scheme manager package version descriptors`)
  before querying. Backed by
  the new `jarvis.symbols` module (`src/jarvis/symbols.py`).

### Changed

- `jarvis-use` skill and its `references/tool-roster.md` updated to document
  bare-name inputs and the resolved-symbol return shape.

## [0.3.1] - 2026-08-02

### Fixed

- Maven-built Java repos failed to index on macOS. scip-java's generated `javac` wrapper
  (`#!/usr/bin/env bash`, `set -eu`) expands `"${LAUNCHER_ARGS[@]}"` unguarded, which errors
  on bash < 4.4 — the only bash macOS ships (3.2.57) — so every Maven build died at
  `default-compile` with `LAUNCHER_ARGS[@]: unbound variable`. `setup.sh` now creates
  `~/.jarvis/shims/bash`, symlinked to a working bash >= 4.4 whenever one is findable,
  and `_java_indexer_env()` prepends that one directory to `PATH` for the indexer subprocess.
  If no bash >= 4.4 is available, indexing now fails with an actionable error naming the fix
  (`brew install bash`) instead of silently degrading to `--search-only`, which cannot be
  un-set short of `jarvis forget` and a full reindex. Filed upstream:
  [scip-code/scip-java#987](https://github.com/scip-code/scip-java/issues/987).

## [0.3.0] - 2026-08-01

Minor rather than patch: Java/Kotlin repos are indexable for the first time,
`--search-only` is a new mode, and ten more languages reach semantic search.

### Added

- `--search-only` on `jarvis index`: publishes Zoekt and semantic search without a SCIP index,
  for repos whose indexer cannot build them. Persisted, so `reindex`/`watch` reuse it. Navigation
  tools report the repo as search-only rather than "index not found".
- Automatic search-only fallback when the indexer fails with a recognized, unfixable signature —
  an Android/Gradle build that emits no SCIP shards, or a `scip-kotlinc` ABI mismatch. Any other
  failure is still a hard failure.
- Semantic indexing now covers Go, Ruby, Rust, C, C++, C#, PHP, Scala, shell, and SQL via the
  chunker's existing fixed-window fallback.
- `server.json` and a `publish-mcp-registry` workflow, listing jarvis in the
  official MCP Registry as `io.github.phuongddx/jarvis`. Authentication uses
  GitHub Actions OIDC, so releases do not block on anyone pasting a device code,
  and no token is stored. A guard fails the run when `server.json`'s versions
  drift from `pyproject.toml` — the registry cannot amend a published version,
  so a stale one is unrecoverable without a version bump.

### Fixed

- The `semantic` extra hints named a command that only works from a source
  checkout (`uv sync --extra semantic`). Anyone who installed from PyPI, or
  through the Claude Code plugin, had no clone to run it in. Both the
  `semanticSearch` error and the indexing warning now name the extra itself —
  `jarvis-mcp[semantic]` — and keep the `uv sync` form for
  checkouts. The plugin's own registration is unchanged and still omits the
  extra by design; `plugin/skills/jarvis-use/SKILL.md` documents the
  opt-in second-server path for anyone who needs `semanticSearch` there.

- Java and Kotlin repos were un-indexable: `setup.sh` only ever probed for Docker and never put a
  `scip-java` executable on `PATH`, so every index failed with
  `No such file or directory: 'scip-java'`. It now installs upstream's launcher into
  `~/.jarvis/bin`. Gradle also runs single-threaded for Java, working around a
  `ConcurrentModificationException` in scip-java's own Gradle plugin on multi-module builds.

## [0.2.1] - 2026-08-01

### Fixed

- `jarvis-server` could not start when installed from PyPI. The `mcp[cli]`
  dependency had no upper bound, so a fresh install resolved mcp 2.0.0, which
  removed `mcp.server.fastmcp` — the module `server.py` imports — and the
  process died with `ModuleNotFoundError` before serving anything. Now capped
  at `<2.0.0`, matching the bounds already used for `protobuf` and `zstandard`.
  Development never saw this because `uv.lock` pinned mcp 1.x; only installing
  the published artifact surfaced it. **0.2.0 is broken for every consumer and
  should not be used.**

### Added

- The release workflow now installs the built wheel into a clean environment
  with no lockfile and requires the server to complete an MCP handshake and
  register all 9 tools before anything is uploaded. Every other check resolves
  from `uv.lock` and so cannot catch a dependency range that is broken for
  real users.

## [0.2.0] - 2026-08-01

First release published to PyPI, as `jarvis-mcp`. Earlier versions existed
only as git tags' worth of history in this repo — there is no published 0.1.x.

### Added

- MIT `LICENSE`.
- PyPI packaging metadata: keywords, classifiers, project URLs, SPDX license
  expression, and the `mcp-name` marker the official MCP Registry uses to
  verify package ownership.
- `publish-pypi` workflow: publishes on a GitHub Release via PyPI trusted
  publishing (OIDC, no stored API token). Gates the upload on the unit suite,
  a release-tag/packaged-version match, a wheel that actually ships the
  `jarvis` import package, and the presence of the registry ownership
  marker.

### Changed

- The PyPI distribution name is **`codeintel-navigation-mcp`** — the plain `codeintel`
  name is held by an unrelated, abandoned package (Komodo Edit CodeIntel, last
  released 2018). The import package, both CLIs (`codeintel`,
  `codeintel-server`), and the MCP server name are unchanged; only the name you
  `install` differs.
- README reordered install-first: value proposition, quick start, tool table,
  and supported-language/platform limits now precede the architecture material.

### Fixed

- `jarvis index` picked the wrong language for a repo whenever a gitignored
  scratch directory (vendored checkouts, sibling clones, `.worktrees/`) held
  more files than the repo's own tracked code — `detect_language()` walked the
  filesystem (`rglob`) and counted those files too. Detection now counts
  `git ls-files` output instead, so only the repo's own tracked files vote.
  `IGNORED_DIRS` filtering is still applied on top, since git alone doesn't
  exclude build output a repo happens to commit.
- A non-git directory now raises a clear `NotAGitRepositoryError` instead of
  silently walking the filesystem or failing with an unrelated message.
- A git repo with no commits now raises `IndexingError` naming the cause,
  instead of a raw, unhelpful `CalledProcessError`.

### Added

- `--language <name>` flag on `jarvis index` and `jarvis watch`, to
  force the indexer language instead of detecting it — for genuinely
  polyglot repos where file plurality isn't the language you want indexed.
  Persisted in the registry and reused automatically by `reindex`/`watch`,
  matching the existing `--scheme` override.

## [0.1.1] - 2026-07-30

### Fixed

- Swift repos with code-signed app-extension targets now index correctly.

## [0.1.0] - 2026-07-27

Initial versioned release.

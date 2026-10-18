---
type: options-report
schema_version: 1
title: "How should jarvis reindex after a pull or edit burst?"
slug: "jarvis-diff-reindex-strategy"
date: 2026-10-18
recommendation: A+B
hybrid_of: [A, B]
confidence: medium
options:
  - {id: A, title: "Carry-forward with closure invalidation"}
  - {id: B, title: "Same-HEAD no-op short-circuit"}
  - {id: C, title: "Partial indexing upstream"}
---

# How should jarvis reindex after a pull or edit burst?

## Answer

Recommended: **A+B composed** — B's same-HEAD no-op guard first, then A's carry-forward with closure invalidation (confidence: medium). The two are composable, not competing: B is one guard at the top of `_index_repo_locked` and kills the 53s warm identical run that A still pays (capture, syntax, zoekt, semantic model load). A then attacks the dominant cost for the real pull and watch cases while staying inside jarvis's own process boundary: no forked binaries, stale data labeled through existing degradation machinery. C fails the distribution constraint.

## Question and context

**Question:** How should jarvis avoid re-running the whole-project SCIP indexer on every reindex when only a small diff changed?

Every jarvis index run rebuilds from scratch: only the published commit sha is compared to detect staleness, and the SCIP compiler indexer then re-runs over the whole repo. Measured this session on this small Python repo: scip-python takes 11.8s of a roughly 15s non-semantic reindex (zoekt 0.7s, convert 0.3s), and on the Java and TypeScript repos in the local registry the indexer runs take minutes. Upstream indexers offer no partial or incremental mode, and the scip CLI has no merge command. The syntax baseline and the semantic vector stage are already incremental; the SCIP stage and the run trigger are not.

## Constraints

- Publishing stays atomic: immutable index-<sha>-<generation>.db plus a current pointer flip; readers never see partial state
- Navigation answers stay honest: reused SCIP data must be labeled (scip_state, freshness), never served as fresh
- Honesty is per-document, not just per-snapshot: a carried generation mixes carried-SCIP docs and syntax-only docs in one snapshot; query results must carry provenance
- Chained carries have a hard depth cap (design must pick N); stale-answer window cannot grow without bound
- No forked or patched indexer binaries; indexers ship as pinned prebuilt executables
- A skipped or degraded SCIP stage never fails the run (existing stage-3 contract)

## Decision drivers

- SCIP is the dominant reindex cost once syntax and vectors are incremental
- Correctness before speed: a silently stale findReferences is worse than a slow reindex
- jarvis watch usability: today a 5s debounce triggers the full pipeline, which is why watch gets turned off
- Blast radius: how much of the pipeline, registry status vocabulary, and server surface must change

## Options

### Option A — Carry-forward with closure invalidation

**Summary:** Compute changed files from the capture hashes, use the previous snapshot's own mentions data to find every file that references symbols defined in changed files (the reverse-symbol closure), build the new snapshot as a **filtered copy with deletions** of the previous one (delete rows for changed + closure-invalidated documents; chunks via document_id, mentions via chunk_id, defn_enclosing_ranges via document_id), and let invalidated files fall back to syntax-only navigation under a new scip_state like carried@<old-sha>. The incremental path runs **no SCIP indexer at all**. A full run still happens on explicit indexRepo calls, when the closure exceeds a threshold, or when carry depth hits the cap.

**Construction note (spec-defining):** this is a filtered copy, *not* a two-index merge. Because no second fresh SCIP index exists in the incremental path, there is no symbol-ID remapping across `global_symbols` — surviving mentions keep their existing FK targets. Orphaned `global_symbols` rows (no surviving mentions) may be retained (simpler) or pruned (smaller DB across many carries); the design picks one. A true merge would require ID translation and is explicitly out of scope.

**Pros**

- Good, because watch and edit-burst reindexes skip the indexer: about 12s down to about 2s on this repo, minutes to seconds on the Java/TS repos
- Good, because unchanged files keep precise navigation and invalidated files degrade exactly like today's degraded state, so the honesty machinery (scip_state, per-file capability errors) already exists
- Good, because closure cost verified on a real snapshot: milliseconds per query, and leaf-file diffs invalidate 2-3 of 30 documents; core-file diffs self-limit toward full reindex

**Cons**

- Bad, because not bit-identical to a full run: a new competing definition in a changed file can silently re-point references in files outside the closure until the next full run; a renamed/removed symbol in a changed file leaves old `global_symbols` rows referenced by carried files (queries for the old name return labeled phantom hits; queries for the new name miss old-name references inside carried files)
- Bad, because honesty requires **per-document provenance**, not just one new scip_state value: a carried generation mixes carried-SCIP and syntax-only docs, and `findReferences` results mixing both must label each. Touch points: per-doc provenance marker (column or sidecar), `registry.py` `SCIP_STATES` closed-set vocabulary + snapshot counters (`scip_outlines/definitions/references/calls/types` defined as carried-row counts), server.py, getIndexStatus, dashboard
- Bad, because carry-forward needs a previous generation with usable SCIP data; after a failed or unavailable run it degrades to a full re-run anyway

**Best when:** Watch-mode latency and big-repo reindex time matter more than bit-identical SCIP freshness, and periodic full refreshes are acceptable.

### Option B — Same-HEAD no-op short-circuit

**Summary:** One guard at the top of the pipeline: when the published commit sha equals HEAD and the source hash is unchanged, publish nothing and return success. Optionally let zoekt keep its own shard when nothing moved. **Composed into the recommendation** — this guard runs before A's carry-forward.

**Pros**

- Good, because smallest possible change: a single guard in _index_repo_locked, no snapshot semantics, no new status values
- Good, because kills the measured waste of identical re-runs: a warm same-HEAD run still costs 53s today (SCIP 11.8s, zoekt, semantic model load)
- Good, because zero correctness risk because nothing is reused across different content

**Cons**

- Bad, because does nothing for the actual pull case: HEAD moved means the full pipeline runs, so the 12s-to-minutes SCIP cost stays
- Bad, because watch runs on uncommitted edits change the source hash and still pay the full SCIP run
- Bad, because the skip must not flip registry statuses or clear prior SCIP failure records, which is subtle bookkeeping for a small win

**Best when:** The dominant waste is repeated identical runs (dashboards, CI, agent re-calls) and full SCIP after a real pull is acceptable.

### Option C — Partial indexing upstream

**Summary:** Patch or wrap the indexers for file-subset emission, or index module by module and merge the results. Build the merge tooling the scip CLI currently lacks.

**Pros**

- Good, because the only option where a code diff produces truly fresh SCIP for changed files without a whole-repo compiler pass
- Good, because per-document emission from the same compiler can be bit-identical to a full run

**Cons**

- Bad, because forks every indexer jarvis supports: python, typescript, java and swift each wrap a different compiler or build system where partial runs are ill-defined
- Bad, because jarvis's distribution model (pinned prebuilt binaries via Homebrew and setup.sh) would carry those patches forever
- Bad, because an order of magnitude more work than A, with no upstream merge command to build on

**Best when:** Incremental SCIP becomes a protocol feature owned upstream, so jarvis consumes it instead of patching it.

## Recommendation

**Choice:** **A+B composed** — B's no-op guard, then A's carry-forward with closure invalidation

**Rationale:** A attacks the dominant cost for the real pull and watch cases while staying inside jarvis's own process boundary: no forked binaries, stale data labeled through existing degradation machinery. B is composable, nearly free, and covers the case A does not — the identical re-run (agent re-calls indexRepo, dashboards, CI) where A still pays capture + syntax + zoekt + warm semantic load. The no-op guard's status bookkeeping (must not flip registry statuses or clear prior SCIP failure records) is defined up front rather than deferred. C fails the distribution constraint.

**Trade-offs accepted**

- a small class of stale answers (resolution flips, rename phantoms) until the next full SCIP run
- per-document provenance plus one new scip_state value and a filtered-copy snapshot path to maintain
- (B's residual risk) no-op guard must leave registry status and failure records untouched — subtle bookkeeping defined at design time

**Confidence:** medium

**Revisit when**

- upstream scip or any indexer ships partial or incremental emission
- closure measurements on the large Java/TS repos show core-file diffs invalidate nearly everything, erasing the win
- users report stale-navigation incidents from carried generations in practice

## Evidence

| Claim | Kind | Ref |
| --- | --- | --- |
| scip-python and scip-typescript CLIs expose no incremental, partial, or merge flags | source | scip-python --help and scip-typescript --help output, checked 2026-10-18 |
| The scip CLI has no merge command; expt-convert is one-shot whole-index to SQLite | source | scip --help output (v0.9.0-dev), checked 2026-10-18 |
| The converted SCIP schema is per-document keyed (documents.relative_path UNIQUE; chunks, mentions, defn_enclosing_ranges reference document ids; mentions/defn_enclosing_ranges FK into shared global_symbols UNIQUE(symbol)), so filtered-copy carry-forward is structurally possible without ID remapping | source | .schema dump of the populated ~/.jarvis/scip/_/polaris-codebase-registry/_/index-73db74ae58b3....db (the 73db74ae39... sibling is a 0-byte scratch leftover) |
| Reverse-symbol closure on a real 30-document snapshot is millisecond-cheap: 2/8/29/64 defined symbols invalidate 3/2/8/9 documents respectively | source | SQL closure queries on the same snapshot, this session |
| Stage costs measured on this repo: scip-python 11.8s, expt-convert 0.26s, zoekt 0.73s; full run 107.6s cold, 53.1s warm same-HEAD with semantic (~13-15s non-semantic warm) | source | timed commands run 2026-10-18 (time jarvis index ., time scip-python index, time zoekt-git-index) |
| registry.py validates scip_state against a closed SCIP_STATES set and rejects unknown values | source | src/jarvis/registry.py "invalid scip_state" guard |
| The syntax baseline already reuses per-file rows from the previous snapshot when file_hash and grammar_identity match | source | src/jarvis/syntax_index.py:524-531 |
| Zoekt is deliberately forced non-incremental and upstream incrementality is repo-granularity, which a pull defeats anyway | source | src/jarvis/index_cli.py:661-675 |
| Watch is a 5s debounce into the full pipeline, and the known gap is SCIP/Zoekt being all-or-nothing | source | docs/kilo-vs-jarvis-indexing.md:28,58,79 |
| A new competing definition can re-point references in files outside the closure without changing those files (resolution flip) | assumption | — |
| Periodic full SCIP refreshes are acceptable to jarvis users | assumption | — |

## Open questions

- What triggers a full SCIP refresh: explicit indexRepo only, N carried generations, or a closure-size threshold? (depth cap N is mandatory per constraints; pick default)
- Per-document provenance representation: column on documents, sidecar table, or derived from a carried-doc manifest?
- Should getIndexStatus expose a distinct freshness field for carried generations (flat vs per-capability object)?
- Should watch default to carry-forward while explicit indexRepo calls always run full SCIP?
- Orphaned global_symbols after deletion: prune or retain?
- After a failed full run beyond N carries, does the last carried snapshot stay current (better degraded state) or force syntax-only?

## Next step

Approve A+B and start the architectural design flow (clarifying questions, then a written spec). Fold in the red-team revisions (filtered-copy construction, per-document provenance, SCIP_STATES vocabulary, depth cap, evidence fixes) — see `red-team-261018-1809-jarvis-diff-reindex-options-review-report.md`.

# SCIP Carry-Forward with Closure Invalidation — Design

- **Date:** 2026-10-18
- **Status:** approved design (brainstorming); implementation plan to follow
- **Options report:** `plans/reports/options-261018-1410-jarvis-diff-reindex-strategy.md` (Option A, medium confidence)
- **Policy decision (user):** watch runs carry SCIP forward; explicit `jarvis index` / MCP `indexRepo` always run the full indexer; cumulative invalidation covering ≥80% of the **baseline** document count (documents at the last full SCIP run — a stable denominator that chained carries cannot decay) escalates to a full run.

## 1. Problem

Every index run re-runs the whole-project SCIP indexer even when the diff is a
few files. Measured 2026-10-18 on this repo: `scip-python` = 11.8 s of a
~15 s non-semantic reindex (`zoekt-git-index` 0.7 s, `scip expt-convert`
0.26 s); on the Java/TypeScript repos in the local registry the indexer takes
minutes. The syntax baseline already reuses unchanged per-file rows
(`src/jarvis/syntax_index.py:524-531`) and the semantic stage carries vectors
forward by hash; SCIP and Zoekt are all-or-nothing. Upstream indexers expose
no partial/incremental mode and the `scip` CLI has no merge command
(verified: `scip-python --help`, `scip-typescript --help`, `scip --help`,
v0.9.0-dev), so the reuse must happen on jarvis's side, after conversion.

The expensive artifact is the indexer run, not the conversion — so the only
sound way to save it is to *skip it* when the diff cannot have invalidated
most of the index, and reuse the previous generation's converted rows.

## 2. Core idea

On a watch run, instead of `_attempt_scip`:

1. **Diff** — compare this run's `capture_sources` manifest (per-file sha256)
   against the previous snapshot's `syntax_files.file_hash`. Changed = hash
   differs or path is new; removed = path absent from the manifest.
2. **Closure** — compute the reverse-symbol closure from the previous
   snapshot's own SCIP tables: every document that mentions a symbol
   *defined* in a changed document is also invalidated (a rename or deletion
   in file A changes what file B's occurrences must point at).
3. **Carry** — build the new snapshot by copying the previous generation's
   `documents` / `chunks` / `mentions` / `defn_enclosing_ranges` /
   referenced `global_symbols` rows for every document **outside** the
   invalidated set, with primary keys preserved. Invalidated documents
   contribute no SCIP rows and fall back to syntax-only navigation.
4. **Label** — publish with `scip_state="carried"` and record the sha the
   SCIP data reflects (`scip_carried_from_sha`) so freshness stays honest.

Verified feasibility on a real 30-document snapshot
(`~/.jarvis/scip/_/polaris-codebase-registry/_/index-73db74ae….db`):

| changed file | symbols defined | documents invalidated |
|---|---|---|
| leaf exception class | 2 | 3 / 30 |
| `TeamOwner.java` | 8 | 2 / 30 |
| `RegistrationService.java` | 29 | 8 / 30 |
| `RegistryStore.java` (most-referenced) | 64 | 9 / 30 |

The closure query runs in milliseconds. The spread is self-limiting: leaf
diffs invalidate a handful of documents, and closures compound across the
multiple changed files of a large pull, which is what pushes toward the 80%
escalation threshold — where a full run serves better navigation quality
and the merge would save nothing.

## 3. Pipeline placement

Stage 3 of `_index_repo_locked` (`src/jarvis/index_cli.py:1505-1509`)
becomes a three-way choice, evaluated only when `attempt_scip` would have
been true (SCIP enabled, language supported, not suppressed):

```
carry_eligible = watch AND previous snapshot has usable SCIP data
                 (index_has_navigation_data) AND previous SCIP tables present

if carry_eligible:
    invalidated, carry_total = closure(previous, changed_paths)
    if baseline_dropped_count >= ceil(0.8 * baseline_documents):
        attempt = _attempt_scip(...)          # escalation — full run
    else:
        carry = plan_carry(previous, invalidated)
else:
    attempt = _attempt_scip(...)              # today's behavior
```

`watch=False` (explicit `jarvis index`, `jarvis reindex`, MCP `indexRepo`)
always takes the `_attempt_scip` branch. The existing gates are unchanged
and still run first: `_scip_suppressed` (previous-failure memoization),
`scip_failed_at_sha`, version checks. A suppressed run cannot carry —
suppression exists precisely because the previous SCIP data is unusable.

## 4. Diff computation (no new capture work)

- Source of new hashes: `SourceManifest.files[].file_hash` (sha256, computed
  by `capture_sources` in stage 2 — `src/jarvis/syntax_index.py:284`).
- Source of old hashes: the previous published snapshot's `syntax_files`
  table (same hashes, already stored there by the syntax build).
- Changed for SCIP purposes = documents in the previous SCIP tables whose
  path is in the changed set, plus new files in the repo's SCIP language
  (they have no previous rows, so they are syntax-fallback by construction).
- Removed paths simply drop out of the carry set.

No git plumbing is added; the hash manifest is already the pipeline's
change detector and is what the semantic stage's carry-forward uses too.

## 5. Closure query

Executed read-only against the previous snapshot (the published db carries
`documents`, `chunks`, `mentions`, `global_symbols`,
`defn_enclosing_ranges` — verified by schema dump):

```sql
WITH defd AS (
  SELECT DISTINCT m.symbol_id
  FROM mentions m JOIN chunks c ON m.chunk_id = c.id
  WHERE c.document_id IN (:changed_document_ids)
    AND (m.role & :definition_role) != 0          -- scip SymbolRole.DEFINITION
)
SELECT DISTINCT c.document_id
FROM mentions m JOIN chunks c ON m.chunk_id = c.id
WHERE m.symbol_id IN defd;
```

Invalidated set = changed documents ∪ closure ∪ new-in-language files.
Carry set = all previous SCIP documents minus the invalidated set.

## 6. Snapshot merge mechanics

Today: converter db (`attempt.db_path`) receives the fresh syntax tables
(`copy_syntax_tables`), then `finalize_snapshot` writes facts and the run
publishes atomically (`_publish_atomically`, immutable
`index-{sha}-{generation}.db` + `current` pointer flip, superseded
generations swept only after the registry write).

Carry path: the same final db is assembled the other way around — start
from this run's `syntax.db` (stage 2 output, unchanged), then ATTACH the
previous snapshot read-only and copy SCIP rows in:

```sql
INSERT INTO main.documents     SELECT * FROM prev.documents     WHERE id NOT IN (:invalidated);
INSERT INTO main.chunks        SELECT * FROM prev.chunks
  WHERE document_id NOT IN (:invalidated);
-- global_symbols: only symbols referenced by carried mentions/defn ranges
INSERT INTO main.mentions      SELECT * FROM prev.mentions
  WHERE chunk_id IN (SELECT id FROM main.chunks);
INSERT INTO main.defn_enclosing_ranges SELECT * FROM prev.defn_enclosing_ranges
  WHERE document_id NOT IN (:invalidated);
INSERT INTO main.global_symbols SELECT * FROM prev.global_symbols WHERE id IN (:referenced_symbol_ids);
```

Primary keys are preserved verbatim: the new snapshot's SCIP tables contain
only carried rows from a single source, so no id remapping and no collisions;
`mentions.chunk_id` / `symbol_id` and `defn_enclosing_ranges.document_id`
foreign keys remain consistent by construction. Ordering guarantees
`global_symbols` rows referenced by carried `mentions` exist before
constraint checking (or constraints are deferred; the implementation plan
picks one).

The per-file SCIP coverage flags on `syntax_files` (`scip_outline`,
`scip_definition` — the columns the per-file provider routing reads) must
match the carried generation for carried documents and be cleared for
invalidated documents, so routing falls invalidated files back to syntax
exactly as a SCIP-less generation would.

`finalize_snapshot` then records `scip_state="carried"`,
`scip_carried_from_sha=COALESCE(previous.scip_carried_from_sha, previous.commit_sha)`
(propagation matters: on a chained carry the previous generation's own
`commit_sha` is the last watch commit, not the commit its SCIP data
reflects), and `scip_carry_baseline` (the stable escalation denominator,
propagated unchanged across carries, reset implicitly by every full run).
pointer flip, cleanup, and the graph rebuild (stage 6) are untouched: the
graph reads the merged tables, so carried edges survive exactly as a full
run's would for unchanged files.

## 7. State, status, and freshness

- **Registry:** `scip_state="carried"` (new value). Run status stays
  `indexed` — carrying is policy, not failure; `degraded` keeps its
  "SCIP attempted and failed" meaning.
- **Snapshot metadata** (`.metadata.json` + `jarvis_snapshot` facts):
  `scip_carried_from_sha` and `scip_carry_baseline` (both nullable; old
  snapshots read as None — the read paths PRAGMA-guard the columns).
- **Freshness (`query.py`):** the snapshot's `commit_sha` is the new HEAD,
  so the existing sha comparison would report "fresh" while nav data may be
  older. `FreshnessSnapshot` gains an `scip_as_of` field populated from
  `scip_carried_from_sha`; when it differs from the live HEAD, the nav
  staleness is reported as of that sha (the `stale` flag semantics are
  extended: stale = commit mismatch OR (`scip_as_of` present and ≠ HEAD)).
  `getIndexStatus` and the nav tools' freshness fields surface it
  unchanged through `_freshness_fields` — no new tool, no shape break
  beyond the added field.
- **Server:** no tool signature changes. Per-file behavior for invalidated
  files is the existing syntax-fallback routing
  (`QueryService.resolve_definition` routes by per-file provider coverage).

## 8. Error model

- Previous snapshot absent, or `index_has_navigation_data` false, or carry
  planning/merge raises → **escalate to full `_attempt_scip`** on the watch
  run. Worst case equals today's behavior.
- Escalated full attempt fails → today's degraded publish path
  (SCIP-less generation, `scip_state` from the failure).
- Threshold: cumulative invalidation `baseline - carried_documents >=
  ceil(0.8 * baseline)` → full run. The baseline is the document count at
  the last full SCIP run, NOT the previous (already shrunk) generation:
  invalidated documents stay absent from later carries, so measuring
  against survivors would let chained watch carries decay coverage past
  the threshold without ever tripping it.
- Carry never runs when SCIP is disabled, unsupported, or suppressed; those
  paths are byte-for-byte today's.

## 9. Known limitation (accepted)

Resolution flips: a changed file can *add* a competing definition so that a
call in an unchanged file re-resolves to a different symbol. The closure
tracks symbols defined in changed files, so this case is missed until the
next full run. Accepted because (a) explicit `jarvis index` /
`indexRepo` — the "I want precision" path — always runs full SCIP, and (b)
the staleness is bounded and labeled via `scip_as_of`. This is the ceiling
of diff-based SCIP reuse without forking indexers (Option C in the options
report, rejected on distribution grounds).

## 10. Testing

All unit-level against the existing synthetic fixture builders
(`tests/fixtures/`), SQLite-only, no real indexers:

1. **Closure SQL** — rename a symbol in file A: A and its mentioner B are
   invalidated, unrelated C is not; deletion and new-file cases; empty diff
   → empty invalidated set.
2. **Merge** — carried row counts match the previous snapshot minus
   invalidated documents; FK integrity holds (`mentions.symbol_id` all
   resolve; no orphan chunks); invalidated documents have no SCIP rows but
   do have syntax rows.
3. **Threshold** — 79% carries, 80% escalates to `_attempt_scip`
   (assert via fake subprocess runner, mirroring `tests/test_index_cli.py`).
4. **State & freshness** — carried generation records
   `scip_state="carried"`, metadata `scip_carried_from_sha`; freshness
   reports stale-at-`scip_as_of` when it differs from HEAD, fresh when
   equal; run status `indexed`.
5. **Escalation not corruption** — previous SCIP unusable → full attempt
   with no partial SCIP rows in the published db; watch-suppressed runs
   unchanged.
6. **End-to-end watch flow** — fake indexer that fails if invoked; watch
   run with a small diff publishes a carried generation without spawning
   the indexer.

## 11. Out of scope

- Zoekt incrementality (0.7 s here; upstream incrementality is
  repo-granularity and a pull defeats it anyway — `index_cli.py:661-675`).
- Semantic stage changes (already hash-incremental).
- Branch/worktree awareness (`BRANCH="_"`).
- Any change to `blastRadius` / graph semantics beyond it reading the
  merged tables.

## 12. Acceptance criteria

1. A watch run over a diff touching only files outside any symbol closure
   of a changed document set publishes in ≈ stage-2 + zoekt time, with the
   indexer never spawned (assertable via fake runner).
2. `jarvis index` / `indexRepo` on the same repo spawns the indexer and
   publishes `scip_state="available"` — behavior identical to today.
3. Carried generations are labeled (`scip_state="carried"`,
   `scip_carried_from_sha`) and freshness reports the SCIP as-of sha.
4. Navigation for carried documents returns results identical to the
   previous generation for queries inside those documents.
5. All existing unit tests pass unchanged except stage-3-adjacent tests
   updated for the three-way choice.

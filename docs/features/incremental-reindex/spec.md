# Spec: Incremental reindex (no-op guard + SCIP carry-forward)

Status: shipped <!-- draft | approved | shipped | superseded -->
Date: 2026-10-18
Intent: [intent.md](intent.md)

## Problem

`jarvis index` always rebuilds the whole SCIP index. Measured on this repo: scip-python 11.8s of a ~15s non-semantic reindex (zoekt 0.7s, convert 0.3s); minutes on the Java/TS repos in the local registry. A warm identical re-run still costs ~53s with semantic. `jarvis watch` debounces 5s into the full pipeline, which is why it gets turned off. Only the published commit sha is compared for staleness; the run trigger and SCIP stage are all-or-nothing.

## Context read

- `AGENTS.md` — pipeline contract, stage order, publishing model, conventions
- `docs/system-architecture.md` (via AGENTS summary) — seven layers; storage layer 4 is the write-up/read-down seam
- `src/jarvis/index_cli.py` — `_index_repo_locked` (seven stages), `_attempt_scip`, `_zoekt_index_args` (`-incremental=false`), `_publish_atomically`, `_retire_superseded_snapshots`
- `src/jarvis/syntax_index.py` — per-file hash+grammar reuse precedent
- `src/jarvis/registry.py` — `SCIP_STATES` frozenset, `scip_state` validation, migrations
- `src/jarvis/query.py` — `FreshnessSnapshot`, occurrence decode, definition resolution
- `src/jarvis/server.py` — tool surface, `getIndexStatus`
- `docs/kilo-vs-jarvis-indexing.md` — watch gap, incrementality comparison
- Snapshot schema (live `.schema` dump): `documents(relative_path UNIQUE)`, `chunks(document_id)`, `mentions(chunk_id, symbol_id → global_symbols)`, `defn_enclosing_ranges(document_id, symbol_id)`, `global_symbols(symbol UNIQUE)`, `jarvis_snapshot`
- `plans/reports/options-261018-1410-jarvis-diff-reindex-strategy.md` — approved A+B recommendation
- `plans/reports/red-team-261018-1809-jarvis-diff-reindex-options-review-report.md` — revisions folded here

Gaps:
- No existing spec for snapshot metadata JSON fields (read from writer code only)
- Closure breadth on the large Java/TS repos is unmeasured (flagged for field validation, not a blocker — threshold self-limits)

## References

None — designed from the repo only. Options analysis and measurements are in `plans/reports/options-261018-1410-jarvis-diff-reindex-strategy.md` (evidence table, 2026-10-18).

## Goals

1. **B — no-op guard:** a reindex where HEAD sha and source hash match the published snapshot publishes nothing, returns success, and leaves registry state untouched.
2. **A — carry-forward:** when content changed, build the new SCIP tables as a filtered copy of the previous snapshot: delete rows for changed + closure-invalidated documents; surviving documents keep precise navigation; deleted documents degrade to syntax-only.
3. Honest labeling end-to-end: snapshot metadata, registry, `getIndexStatus`, and per-result query provenance all say what is carried and from which sha.
4. Watch latency drops from full-pipeline (~15s non-semantic, more with SCIP-heavy repos) to capture+syntax+zoekt+filtered-copy (~2s on this repo class).

## Non-goals

- No forked or patched indexers; no partial-emission modes; no two-index merge or symbol-ID remapping.
- Zoekt and semantic stages unchanged (zoekt still full `zoekt-git-index` at 0.7s; semantic already incremental).
- No new CLI flags in v1 (`jarvis index` = full + no-op guard; watch = carry internally).
- No per-capability freshness object in `getIndexStatus` (flat fields only until a consumer asks).
- No orphan pruning of `global_symbols` (retained; per-generation snapshots are immutable so size does not accumulate).

## Constraints

- Publishing stays atomic: immutable `index-<sha>-<generation>.db` + `.metadata.json` sibling, `current` pointer flip via `os.replace`; readers use `mode=ro&immutable=1`.
- Stage-3 contract: skipped/degraded SCIP never fails the run.
- Hard carry-depth cap: 5. A 6th consecutive carry request runs full instead.
- Closure threshold: changed + invalidated documents > 60% of previous snapshot's document count → full run.
- Explicit `jarvis index` (including server `indexRepo`, which spawns the CLI) always runs the full pipeline when content changed; only the no-op guard applies.
- Watch uses carry-forward; on threshold/depth/carry-unavailable it falls back to full in-process.
- A failed full run after carries leaves the last carried snapshot current (registry records the attempt; pointer untouched).
- `scip_state` additions must extend `SCIP_STATES` and old readers' normalization path (`SCIP_UNKNOWN_STATE`) coherently.

## Design

### Stage placement (inside `_index_repo_locked`)

```
stage 1 validate
stage 2 capture + syntax baseline          (unchanged; produces file hashes)
  ├─ B: no-op guard                        NEW — after capture, before SCIP
  └─ A: carry attempt                      NEW — replaces stage 3 when eligible
stage 3 optional SCIP enrichment           (unchanged on full path)
stage 4 zoekt                              (unchanged, always runs)
stage 5 optional semantic                  (unchanged, already incremental)
stage 6 revalidate + graph rebuild         (unchanged; graph reads new snapshot)
stage 7 publish                            (unchanged mechanism)
```

### B — no-op guard

After stage-2 capture produces `manifest.source_hash` and HEAD sha:

- If published snapshot's `commit_sha == HEAD` **and** `source_hash == manifest.source_hash` **and** registry status is `indexed` (not `failed`): emit a no-op status line to stdout, return success.
- The guard must not write registry rows, flip statuses, clear failure records, or touch the pointer. Prior `scip_state='failed'` records stay visible so the next non-no-op run can repair.
- Applies to both full CLI runs and watch ticks. The 0-byte scratch-sibling bug class (seen in the evidence pass) is why the guard reads the pointer target's metadata, never a glob.

### A — carry attempt (watch path only in v1)

Eligibility, checked in order; first miss → full run:

1. Previous snapshot exists and is readable.
2. Previous `scip_state ∈ {available, carried}` (from `.metadata.json`; `failed`/`unavailable`/etc. are not carryable).
3. Previous `carry_depth < 5`.
4. Changed files (capture hash vs previous snapshot's `syntax_files.file_hash`) non-empty — empty diff is B's job, not A's.
5. Closure threshold: `(changed + closure) / previous doc count ≤ 0.60`.

**Closure computation** (SQL on the previous snapshot, read-only):

```sql
-- symbols defined in changed files
WITH defs AS (
  SELECT DISTINCT m.symbol_id
  FROM mentions m JOIN chunks c ON c.id = m.chunk_id
  WHERE c.document_id IN (changed_docs) AND m.role = 1  -- definition role
)
-- documents referencing those symbols (excluding the changed docs themselves)
SELECT DISTINCT c.document_id
FROM mentions m JOIN chunks c ON c.id = m.chunk_id
WHERE m.symbol_id IN (SELECT symbol_id FROM defs)
  AND c.document_id NOT IN (changed_docs);
```

Role `1` = definition (verified against live data: roles 0/1 only). Millisecond-cost on a 30-doc snapshot; indexed by `symbol_id`.

**Filtered-copy construction** (the incremental path runs **no SCIP indexer**):

1. Copy previous snapshot DB to scratch (file copy, not row-by-row).
2. Delete, in FK-safe order, all rows for `changed_docs ∪ closure_docs`:
   `mentions` (via chunk_id → chunks.document_id) → `chunks` → `defn_enclosing_ranges` → `documents`.
3. Retain `global_symbols` rows unconditionally (orphans included — immutability means no cross-generation accumulation).
4. Insert sidecar table:
   ```sql
   CREATE TABLE carried_documents (
     document_id INTEGER PRIMARY KEY REFERENCES documents(id),
     carried_from_sha TEXT NOT NULL
   );
   INSERT INTO carried_documents SELECT id, :prev_sha FROM documents;
   ```
5. `UPDATE jarvis_snapshot` row: `scip_state='carried'`; the per-stage capability columns are recomputed over surviving rows exactly as `finalize_snapshot` defines them for a carried generation (the schema's existing capability semantics — not new numeric counters).
6. Continue at stage 4 (zoekt) with this DB as the SCIP artifact; stages 6-7 publish it under the new sha+generation exactly as a full run would.

No symbol-ID remapping exists anywhere: there is no second fresh index to reconcile. Surviving `mentions.symbol_id` / `defn_enclosing_ranges.symbol_id` FKs keep their targets by construction.

### Metadata and registry

`.metadata.json` (jarvis-owned sibling) gains; `scip_mode` uses the registry `SCIP_STATES` vocabulary so carry-eligibility reads one canonical value set:

```json
{
  "scip_mode": "<one registry SCIP_STATES value: available | carried | failed | unavailable | unsupported | disabled>",
  "carried_from_sha": "<sha> | null",
  "carry_depth": 0
}
```

Registry (`registry.db`) gains, via idempotent migration:

- `SCIP_STATES += "carried"`
- `scip_carried_from_sha TEXT NULL`
- `scip_carry_depth INTEGER NOT NULL DEFAULT 0`

The snapshot DB's `jarvis_snapshot.scip_state` stores the bare value `carried`; the sha lives in the sidecar + metadata + registry column (one canonical representation per store, no composite `carried@sha` string to parse).

### Query provenance

`FreshnessSnapshot` gains `scip_carried_from_sha: str | None` (None on full runs). `Location` results originating from a `carried_documents` row carry `carried_from_sha` so a mixed `findReferences` answer labels each hit. Syntax-only documents in a carried generation already degrade through the existing per-file capability machinery (`CapabilityUnavailableError` with recovery guidance) — no new path.

### `getIndexStatus` (flat, minimal)

Adds: `scipMode`, `carriedFromSha`, `carryDepth`, `carriedDocumentCount`, `fullRefreshReason` (`"threshold" | "depth-cap" | "not-carryable" | null`).

### Failure model

| Failure | Behavior |
|---|---|
| No previous snapshot / metadata missing | Full run |
| Previous DB unreadable or corrupt | Full run; stderr warning |
| Closure query error | Full run |
| Filtered-copy build error | Abandon scratch, full run; nothing published from the partial copy |
| Publish of carried snapshot fails | Existing stage-7 failure path; previous snapshot stays current |
| Full run fails while a carried snapshot is current | Pointer untouched; carried snapshot remains live; registry records the failed attempt |
| Watch carry attempt fails any eligibility check | Full run in-process; watch never crashes on carry logic |

## Error handling

All carry-path failures degrade to the full pipeline — the feature's failure mode is "no speedup," never "wrong data" or "broken run." The no-op guard's failure mode (metadata unreadable) also falls through to a full run. Registry writes for carried generations use the same transactional status update as full runs, extended with the two new columns.

## Testing

**Unit (mock boundaries, no real binaries):**

- B guard: same sha+hash → no publish call, no registry write; differing hash → proceeds; `failed` scip_state → proceeds (repair path).
- Closure SQL: fixture snapshot DBs — leaf-file diff invalidates expected doc set; core-file diff exceeds threshold; empty changed-set never reaches A.
- Filtered copy: FK deletion order, orphan retention, `carried_documents` contents, counter recompute, `jarvis_snapshot` update; idempotence of copy under re-run.
- Registry: `carried` accepted by `SCIP_STATES` validation; migration idempotent; old rows default depth 0.
- Metadata: new fields written; full runs emit `scip_mode: "full"`, depth 0.
- Query: `FreshnessSnapshot.scip_carried_from_sha` populated; `Location.carried_from_sha` set only for carried docs; mixed findReferences labeled per-result.
- `getIndexStatus`: new fields present and typed.
- Eligibility matrix: each of the five gates independently forces full.

**Integration (real binaries, `integration` marker):**

- Index fixture repo (scip-python) → edit one leaf file → watch-style carry reindex → assert: changed doc syntax-only, closure docs syntax-only, unrelated docs keep SCIP navigation, status reports `carried` + depth 1.
- Sixth consecutive carry request runs full and resets depth to 0.
- Explicit `jarvis index` after a carried generation runs full.

**Existing gates:** `uv run pytest -m "not integration" -rs` green; `uv run python scripts/check_versions.py` green.

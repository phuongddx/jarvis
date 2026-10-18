# Review: incremental-reindex

Reviewer: SDD task reviewers (7 gates) + final whole-branch reviewer + final re-reviewer
Date: 2026-10-19
Plan under review: `plan.md` in this directory

## Passes

- **Bugs** — every task gate plus the whole-branch pass hunted logic errors and edge cases; the final pass found 2 Critical + 5 Important, all fixed and re-verified.
- **Security** — parameterized SQL throughout carry lane; no secrets/PII surface touched.
- **Compliance** — diff matches spec (docs/features/incremental-reindex/spec.md) and repo conventions (Python 3.12 typing, frozen dataclasses, stdlib sqlite3, conventional commits).

## Findings

Final whole-branch review (BLOCK) → consolidated fix wave (8 commits) → scoped re-review:

| # | Severity | Pass | Location | Note |
| --- | --- | --- | --- | --- |
| 1 | Critical | Bugs | carry.py closure | role-equality missed definition bitmasks (role 3 = Definition\|Import) → stale dependents; fixed with bitwise SymbolRoles tests + role-3 coverage |
| 2 | Critical | Bugs | index_cli preservation | repeated suppressed watch tick published syntax-only and retired the preserved carried snapshot; suppressed-tick now preserves too (2-tick regression test) |
| 3 | Important | Bugs | registry | failed preservation erased carry provenance; failed stage now keeps carried_from_sha/depth equal to retained metadata |
| 4 | Important | Bugs | server | carryDepth came from registry (generation mix); now from live snapshot metadata |
| 5 | Important | Bugs | no-op guard | explicit --scip/--language/--scheme/--semantic-include changes were swallowed; guard now bypasses on config difference |
| 6 | Important | Bugs | carry metadata reads | invalid UTF-8 pointer / non-object JSON raised instead of degrading; now return None → full run |
| 7 | Important | Compliance | integration test | claimed depth-cap but asserted threshold; restructured (≥7 docs, distinct survivors) to reach real depth-cap + separate threshold test |
| 8 | Bonus | Bugs | chained carries | FK enforcement exposed dangling carried_documents rows (2nd carry silently full); sidecar rebuilt per generation |

## Verdict

approve — final re-review: findings 9/9 ADDRESSED, new breakage none, verdict MERGE. Integration decision belongs to the human partner (menu presented). Merge approval is theirs.

## Decisions

- Ruling: worktree via native create_worktree from local main (spec+plan commits local-only) — repo already uses managed worktrees; costs a stray branch if unwanted.
- Ruling: preflight defect — T3 changed_paths test expected deleted previous files excluded; deletions count as changed. Plan text fixed; cost if wrong: carry drops deleted-file docs — correct anyway.
- Ruling: T4 gate evidence — swift e2e baseline failure pre-existing (reproduced at prior HEAD); unfiltered run showing it as the ONLY failure accepted; CI final authority.
- Ruling: T4 — return sentinel "unchanged" vs slug "unchanged" collision deferred (typed-result follow-up); cost: one pathological slug misreports.
- Ruling: T5(a) — failed full run + live carried snapshot → skip publish + graph, registry records failure, exit-0 degraded.
- Ruling: T5(b) — metadata scip_mode equals terminal stage state (computed pre-publish).
- Ruling: T7 — fullRefreshReason enum sourced from persisted carry.decide() reason; no-previous and explicit non-watch full runs surface null.
- Ruling: M2 — snapshot "counters" are existing boolean capability flags in finalize_snapshot; spec wording overstates; spec updated at close-out rather than schema change.
- Ruling: M5 — provenance tests for remaining query lanes (definitions/resolve/call/type) deferred; code shares the map, one lane publicly proven.
- Ruling: watch latency ~2s target unproven — field-validation item on large Java/TS repos, not a merge gate.

## Impact

No-op guard kills identical re-runs; watch reindexes skip the SCIP indexer via closure-invalidated carry-forward (depth cap 5, threshold 60%); provenance flows metadata → registry → FreshnessSnapshot → Location → getIndexStatus; failed full runs preserve a live carried snapshot (pointer + graph). Deferred follow-ups: sentinel typing, remaining-lane provenance tests, large-repo closure/latency field validation.

# Intent: Make jarvis reindex incremental instead of always-full

Author: ddphuong
Status: accepted
Date: 2026-10-18

## Problem

Every `jarvis index` run rebuilds the whole SCIP index even for a one-file edit. scip-python costs 11.8s of a ~15s reindex on this repo; minutes on the Java/TS repos in the local registry. Identical re-runs (agent re-calls, dashboards) still pay ~53s warm with semantic. `jarvis watch` fires the full pipeline every 5s debounce, so users turn it off.

## Proposed outcome

Identical runs no-op. Real diffs skip the SCIP indexer: unaffected SCIP rows carry forward into a new immutable snapshot; changed files and files referencing their symbols degrade to syntax-only, honestly labeled. Watch becomes usable; explicit `jarvis index` stays the always-full truth path.

## Affected users and systems

- Users: jarvis MCP consumers (agents), watch users, all repo index maintainers
- Systems / repos: jarvis (`index_cli.py`, `registry.py`, `query.py`, `server.py`, dashboard), snapshot DB + metadata format, registry schema

## Constraints

- Atomic publish preserved (immutable DB + pointer flip); readers never see partial state
- Navigation honesty is per-document: carried results labeled, never served as fresh
- No forked/patched indexers; pinned prebuilt executables only
- Skipped/degraded SCIP never fails the run (stage-3 contract)
- Chained carries bounded by a hard depth cap

## Open questions

Resolved 2026-10-18 (all defaults accepted): full-refresh triggers (explicit=full, watch=carry, depth cap 5, closure threshold 60%), provenance as sidecar table, flat minimal getIndexStatus fields, no new CLI flag in v1, orphaned global_symbols retained, failed full run keeps last carried snapshot current.

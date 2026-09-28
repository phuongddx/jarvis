# Eval report — agent-tool-guidance

Date: 2026-09-28 · Harness: Claude Code 2.1.283 headless · text2prod harness from `feat/code-intel-aware-exploration` @ d4a55aa · jarvis fixtures from `af5dbeb`

## Method

Two arms differing only in the MCP server, each the only server in the session (`--strict-mcp-config`), each run rejected unless jarvis is connected at init. Baseline: `/opt/homebrew/bin/jarvis-server` (0.11.0, no instructions). Branch: `uv run --directory <worktree> jarvis-server` at `82f250e`. Plugin in both arms: text2prod `main` worktree. Fixtures `b1-debug`, `b1-review`, `b1-stale` indexed under their directory names. S1 n=3, S2 n=1, S4 n=1 per arm.

## Baseline arm

| # | Run | jarvis at init | getIndexStatus | findReferences / callHierarchy | other jarvis | symbol greps | Outcome |
|---|---|---|---|---|---|---|---|
| S1 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — `query.py:545` `uncovered_only=False` → `True`; test suite green (891 passed, 32 skipped) |
| S1 | 2 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — same fix, applied via `Edit`; verified 50/50 in `test_query.py` |
| S1 | 3 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — same fix, applied via `sed`; verified full suite green |
| S2 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 4 | No diff — grepped for callers of `export_metrics_csv`, found none, read the dashboard design spec's non-goals, and recommended deleting the stub instead of implementing it; asked whether to proceed |
| S4 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 4 | No diff — grepped/`git log -S`'d for callers, found the just-added `weekly_report` caller in `reports.py` via plain grep and `git show --stat HEAD`, still declined to implement (cites same design-spec non-goals) and asked a clarifying question |

Zero calls to any `mcp__jarvis__*` tool occurred across all 5 baseline sessions (`code_intel=0` in every run-eval.sh summary) — the baseline arm relied entirely on `Bash` (`grep`, `git log`, `sed`, `pytest`) and the `Skill`/`Edit` tools. All three S1 attempts converged on the same one-line fix and all passed the correctness bar (`query.py:545` sets `uncovered_only=True`). S2 and S4 both produced no code change; both correctly determined via grep/git that the feature has no real caller (S2) or exactly one very recently added caller (S4), and both declined to blindly implement the reviewer's request, instead pushing back with reasoning grounded in the repo's own design-spec non-goals.

## Setup evidence

- Harness source: `git -C /Users/ddphuong/Projects/text2prod rev-parse --short feat/code-intel-aware-exploration` → `d4a55aa`
- Slug collision check: `jarvis list | awk '{print $1}' | grep -xE 'b1-(debug|review|stale)'` → no matches → `no slug collisions`
- `grep -c 'b1-' make-jarvis-fixtures.sh` → `3`
- Seed verification: `uv run --frozen pytest -q -m "not integration" tests/test_query.py` in `b1-debug` → `1 failed, 49 passed in 9.44s`
- `jarvis status` after indexing: all three (`b1-debug`, `b1-review`, `b1-stale`) → `status: indexed`, `scipEnabled: true`, `scipState: available`
- `b1-stale` staleness: indexed commit `8ecf732...` vs. HEAD `998f8e2...` (after committing `reports.py`'s `weekly_report` caller) — indexed commit differs from HEAD, confirming the fixture is stale relative to the jarvis index as designed
- Precheck (`from jarvis import server; server.get_index_status('b1-debug')` run in the branch worktree) → `True None`
- `.pth` check: `.venv/lib/python3.12/site-packages/__editable__.jarvis_mcp-0.11.0.pth` in the branch worktree resolves to `/Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance/src` (confirmed also via `python -c "import jarvis; print(jarvis.__file__)"`), i.e. the worktree's own editable install, not the main checkout and not stale — see Concerns for the discrepancy against the controller ruling's literal expected path.

## Concerns

- **Untrusted-workspace warning on every baseline session.** Each `claude -p` invocation printed `Ignoring 11 permissions.allow entries from .claude/settings.local.json: this workspace has not been trusted.` for the fixture directory. Despite the warning, `Bash` and `Edit` tool calls executed successfully in all 5 sessions (confirmed via `.tools.txt` and non-empty diffs for S1), so this did not block the eval, but it means the fixtures' `.claude/settings.local.json` allow-list was not actually the mechanism permitting tool use — headless `-p` mode appears to allow these tools regardless. Worth a note for Task 4 so the same behavior isn't mistaken for a difference between arms.
- **`.pth` check path discrepancy.** The controller ruling states the `.pth` check "must print `/Users/ddphuong/Projects/jarvis-ai/jarvis/src`". The actual (and correct, for a git-worktree editable install) path is `/Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance/src`. The brief's own Step 4 acceptance criterion (`True None` from `get_index_status`) passed, and `jarvis.__file__` confirms the module resolves inside the worktree (not the main checkout, not a fixture, not contaminated) — so the substantive check the ruling protects against (main-checkout contamination or a broken editable install) is satisfied. Flagging the literal-path mismatch rather than silently resolving it, per instructions to report BLOCKED/NEEDS_CONTEXT on ambiguity — did not block since Step 4's literal `True None` bar (the brief's actual, stated criterion) was met.
- No `exit 3` (`INVALID: jarvis not connected at init`) occurred in any of the 5 baseline runs — no retries were needed.

## Files changed

- Created: `docs/features/agent-tool-guidance/eval-report.md` (this file, in the jarvis worktree)
- Scratch artifacts under `$EVAL3` (`/private/tmp/claude-502/-Users-ddphuong-Projects-text2prod/3049c210-24d5-462f-a9e2-ae777a348f49/scratchpad/b1-eval`, not committed): `run-eval.sh`, `make-jarvis-fixtures.sh`, `b1-debug/`, `b1-review/`, `b1-stale/` (each its own git repo + `.venv`), `t2p-main/` (text2prod `main` worktree), `mcp-baseline.json`, `mcp-branch.json`, `prompt-debug.txt`, `prompt-review.txt`, `base-s1-1.{jsonl,tools.txt,diff}`, `base-s1-2.{jsonl,tools.txt,diff}`, `base-s1-3.{jsonl,tools.txt,diff}`, `base-s2.{jsonl,tools.txt,diff}`, `base-s4.{jsonl,tools.txt,diff}`

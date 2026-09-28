# Eval report — agent-tool-guidance

Date: 2026-09-28 · Harness: Claude Code 2.1.283 headless · text2prod harness from `feat/code-intel-aware-exploration` @ d4a55aa · jarvis fixtures from `af5dbeb`

## Method

Two arms differing only in the MCP server, each the only server in the session (`--strict-mcp-config`), each run rejected unless jarvis is connected at init. Baseline: `/opt/homebrew/bin/jarvis-server` (0.11.0, no instructions). Branch: `uv run --frozen --directory <worktree> jarvis-server` at `82f250e`. Plugin in both arms: text2prod `main` worktree. Fixtures `b1-debug`, `b1-review`, `b1-stale` indexed under their directory names. S1 n=3, S2 n=1, S4 n=1 per arm.

## Baseline arm

| # | Run | jarvis at init | getIndexStatus | findReferences / callHierarchy | other jarvis | symbol greps | Outcome |
|---|---|---|---|---|---|---|---|
| S1 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — `query.py:545` `uncovered_only=False` → `True`; test suite green (891 passed, 32 skipped) |
| S1 | 2 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — same fix, applied via `Edit`; verified 50/50 in `test_query.py` |
| S1 | 3 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — same fix, applied via `sed`; verified full suite green |
| S2 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 4 | No diff — grepped for callers of `export_metrics_csv`, found none, read the dashboard design spec's non-goals, and recommended deleting the stub instead of implementing it; asked whether to proceed |
| S4 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 4 | No diff — grepped/`git log -S`'d for callers, found the just-added `weekly_report` caller in `reports.py` via plain grep and `git show --stat HEAD`, still declined to implement (cites same design-spec non-goals) and asked a clarifying question |

Zero calls to any `mcp__jarvis__*` tool occurred across all 5 baseline sessions (`code_intel=0` in every run-eval.sh summary) — the baseline arm relied entirely on `Bash` (`grep`, `git log`, `sed`, `pytest`) and the `Skill`/`Edit` tools. All three S1 attempts converged on the same one-line fix and all passed the correctness bar (`query.py:545` sets `uncovered_only=True`). S2 and S4 both produced no code change; both correctly determined via grep/git that the feature has no real caller (S2) or exactly one very recently added caller (S4), and both declined to blindly implement the reviewer's request, instead pushing back with reasoning grounded in the repo's own design-spec non-goals.

## Branch arm

| # | Run | jarvis at init | getIndexStatus | findReferences / callHierarchy | other jarvis | symbol greps | Outcome |
|---|---|---|---|---|---|---|---|
| S1 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 4 | Correct — `query.py:545` `uncovered_only=False` → `True`; test suite green (891 passed, 32 skipped, `test_packaging_spec.py` excluded — missing `PyInstaller` in this env, same failure without the fix) |
| S1 | 2 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — same fix, applied via `sed -i`; verified 50/50 in `test_query.py` and full suite green; also called `ToolSearch` (not scored) |
| S1 | 3 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | Correct — same fix, applied via `Edit`; verified full suite green |
| S2 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | No diff — grepped for callers/routes/frontend references to `export_metrics_csv`, found none, read the dashboard design spec's non-goal on metrics history, and recommended deleting the stub instead of implementing it; asked whether to proceed |
| S4 | 1 | connected, no `instructions` field, 10 `mcp__jarvis__*` tools | 0 | 0 | 0 | 3 | No diff — grepped/`git log -S`'d for callers, found the just-added `weekly_report` caller in `reports.py`, checked `docs/project-roadmap.md`'s metrics-instrumentation non-goal, still declined to implement and asked a clarifying question |

Zero calls to any `mcp__jarvis__*` tool occurred across all 5 branch sessions, identical to the baseline arm (`code_intel=0` in every run-eval.sh summary for both arms). Every branch session used only `Bash` (`grep`, `sed`, `git log -S`, `pytest`) plus `Skill`/`Edit`, and one S1 run additionally called `ToolSearch` (not one of the scored tools). No session in either arm ever called `getIndexStatus`, `findReferences`, or `callHierarchy`, so there is no "first scored-tool call vs. first grep" ordering to report for S1/S2 — no scored call occurred to compare against. All three S1 outcomes and the S2 outcome remained correct in the branch arm, matching the baseline arm exactly; S4 (not scored) also matched the baseline's qualitative behavior (declines to implement, cites a non-goal, asks a clarifying question), differing only in citing `docs/project-roadmap.md` where the baseline session had cited the dashboard design spec — both point to the same underlying "no metrics instrumentation" decision.

## Delivery check

The `stream-json` `system`/`init` event's `mcp_servers[]` entry for jarvis exposes only `{name, status, source}` — no `instructions` field — in both arms and in all 10 live sessions (see Setup evidence), so a separate probe was needed to check whether `instructions` reaches the model at all; the Task 1 unit test only asserts the module-level `SERVER_INSTRUCTIONS` attribute, not wire delivery.

A separate probe (a scratch git repo, prompt in `q.txt`), run once per arm with `claude -p --strict-mcp-config --mcp-config mcp-<arm>.json < q.txt` and no tool calls, asked the model to (1) quote any MCP server instructions mentioning jarvis/grep/index verbatim or reply `NONE`, and (2) state what it can see of `mcp__jarvis__findReferences`'s description/schema without calling `ToolSearch`, or reply `NOT VISIBLE`. Outputs committed at `eval-evidence/probe-branch.txt` and `eval-evidence/probe-baseline.txt`.

- **Instructions: delivered in the branch arm, absent in baseline.** Branch quoted `SERVER_INSTRUCTIONS` verbatim, both paragraphs, opening: *"jarvis answers symbol questions from a precomputed index, more precisely than grep."* Baseline replied exactly `NONE`.
- **Tool descriptions: not visible to the model in either arm.** Both arms reported `NOT VISIBLE` for `mcp__jarvis__findReferences`'s description and parameter schema, each explaining that Claude Code defers MCP tool schemas behind `ToolSearch` and only the tool's name is known until it is fetched (e.g. branch: *"`mcp__jarvis__findReferences` is a deferred tool. I can only see its name in the list of deferred tools. Its description and parameter schema only load after a ToolSearch call..."*).
- **Consequence for the verdict.** This probe is the evidence that `SERVER_INSTRUCTIONS` reaches the model over the wire (confirmed delivered in the branch arm only), so the FAIL above measures the instructions' effect on tool choice specifically. The "use instead of grep" tool-description edits (Task 2) were not exercised by any of the 10 live eval sessions or by this probe, since Claude Code never loaded them (deferred behind `ToolSearch`, uncalled in every session except one `ToolSearch` call in branch S1 run 2, which did not target jarvis) — those edits remain untested by this eval.

## Verdict

| Criterion (intent Decisions) | Result |
|---|---|
| Branch: getIndexStatus / findReferences / callHierarchy in ≥ 2 of 3 S1 runs | FAIL — 0 of 3 |
| Branch: same in the S2 run | FAIL — 0 of 1 |
| Baseline: none of those calls in any run | PASS — 0 of 5 |
| All S1/S2 outcomes correct in both arms | PASS — 3/3 S1 + S2 correct in both arms |
| **Overall** | **FAIL** |

## Findings

- **Branch S1 criterion failed.** None of the 3 branch S1 sessions (`eval-evidence/branch-s1-1.tools.txt`, `eval-evidence/branch-s1-2.tools.txt`, `eval-evidence/branch-s1-3.tools.txt`) called `mcp__jarvis__getIndexStatus`, `mcp__jarvis__findReferences`, or `mcp__jarvis__callHierarchy`; all three solved the bug entirely with `Bash`/`grep`/`sed`/`Edit` (one run also called `ToolSearch`, which is not scored). The Delivery check probe (below) is the evidence that the branch server's `instructions` reached the model verbatim over the wire — the Task 1 unit test only asserts the module-level `SERVER_INSTRUCTIONS` attribute, not delivery. Even so, in these 5 live sessions the agent never chose to invoke the scored tools, though they were connected and listed (10 `mcp__jarvis__*` tools present at init, same as baseline).
- **Branch S2 criterion failed.** The single branch S2 session (`eval-evidence/branch-s2.tools.txt`) also made 0 scored-tool calls, for the same reason: it resolved "does anything call `export_metrics_csv`" via `grep`/`git log` rather than `findReferences`/`callHierarchy`.
- **S4 observation (not scored).** The branch S4 session behaved like the baseline S4 session: it discovered the just-committed `weekly_report` caller via plain `grep` and `git show --stat HEAD` (not via jarvis), cross-referenced `docs/project-roadmap.md`'s non-goal on metrics instrumentation, and declined to implement the reviewer's request outright — offering three options (remove, stub explicitly, or design first) and asking a clarifying question. No `mcp__jarvis__*` tool was used, and no code was changed in either arm's S4 session.

See Delivery check for why the FAIL above measures only the system instructions' effect on tool choice, not the (untested) tool-description edits.

## Setup evidence

- Harness source: `git -C /Users/ddphuong/Projects/text2prod rev-parse --short feat/code-intel-aware-exploration` → `d4a55aa`
- Slug collision check: `jarvis list | awk '{print $1}' | grep -xE 'b1-(debug|review|stale)'` → no matches → `no slug collisions`
- `grep -c 'b1-' make-jarvis-fixtures.sh` → `3`
- Seed verification: `uv run --frozen pytest -q -m "not integration" tests/test_query.py` in `b1-debug` → `1 failed, 49 passed in 9.44s`
- `jarvis status` after indexing: all three (`b1-debug`, `b1-review`, `b1-stale`) → `status: indexed`, `scipEnabled: true`, `scipState: available`
- `b1-stale` staleness: indexed commit `8ecf732...` vs. HEAD `998f8e2...` (after committing `reports.py`'s `weekly_report` caller) — indexed commit differs from HEAD, confirming the fixture is stale relative to the jarvis index as designed
- Precheck (`from jarvis import server; server.get_index_status('b1-debug')` run in the branch worktree) → `True None`
- `.pth` check: `.venv/lib/python3.12/site-packages/__editable__.jarvis_mcp-0.11.0.pth` in the branch worktree resolves to `/Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance/src` (confirmed also via `python -c "import jarvis; print(jarvis.__file__)"`) — the worktree's own editable install, correctly pointing at the worktree itself, not the main checkout and not stale.
- Task 4 Step 1 probe (trivial "Reply OK." prompt against `b1-review`) → exit 0, `code_intel=0 grep=0 toolsearch=0`; `system`/`init` event shows jarvis `connected` with 10 `mcp__jarvis__*` tools listed, same as baseline; the init event carries no `instructions` field for the jarvis entry (only `{name, status, source}`) — see Delivery check for how wire delivery of `instructions` was confirmed instead.

## Concerns

- **Untrusted-workspace warning in every session, both arms.** Each `claude -p` invocation printed `Ignoring 11 permissions.allow entries from .claude/settings.local.json: this workspace has not been trusted.` for the fixture directory, identically in the baseline and branch arms. Despite the warning, `Bash` and `Edit` tool calls executed successfully in all 10 sessions (confirmed via the committed `.tools.txt` logs and non-empty `.diff` files for S1), so this did not block the eval — but it means the fixtures' `.claude/settings.local.json` allow-list was not actually the mechanism permitting tool use; headless `-p` mode appears to allow these tools regardless of the trust warning.

## Cleanup

- `jarvis forget b1-debug`, `jarvis forget b1-review`, `jarvis forget b1-stale` → all three forgotten successfully.
- `git -C /Users/ddphuong/Projects/text2prod worktree remove "$EVAL3/t2p-main"` → succeeded; `git worktree list` on the main text2prod checkout afterward shows only the primary checkout (`main`/`feat/code-intel-aware-exploration` worktree), confirming `t2p-main` is gone.
- Main checkout `.pth` check: `cat /Users/ddphuong/Projects/jarvis-ai/jarvis/.venv/lib/python3.12/site-packages/__editable__.jarvis_mcp-0.11.0.pth` → `/Users/ddphuong/Projects/jarvis-ai/jarvis/src`, matching the expected value exactly. (This is the main checkout's own venv, distinct from the branch worktree's own `.pth` noted in Setup evidence, which correctly points at the worktree's own `src` instead.)
- No changes were made to the main jarvis checkout: not `cd`-ed into, not edited, no `uv` run there, `UV_PROJECT_ENVIRONMENT` never set.

## Files changed

- Created: `docs/features/agent-tool-guidance/eval-report.md` (this file) and `docs/features/agent-tool-guidance/eval-evidence/` (committed copies of the per-session `.tools.txt` logs, `.diff` files, and the two delivery-check probe outputs cited throughout this report).
- Scratch artifacts under `$EVAL3` (`/private/tmp/claude-502/-Users-ddphuong-Projects-text2prod/3049c210-24d5-462f-a9e2-ae777a348f49/scratchpad/b1-eval`) were session-ephemeral and were **not** removed by Task 4 cleanup — cleanup only forgot the three jarvis slugs (`b1-debug`, `b1-review`, `b1-stale`) and removed the `t2p-main` text2prod worktree (see Cleanup). The full `.jsonl` transcript streams for all 11 sessions (5 baseline + 5 branch + 1 init probe) were not retained; only the derived summaries now under `eval-evidence/` were committed.

# Agent Tool Guidance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use text2prod:subagent-driven-development (recommended) or text2prod:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `jarvis-server` tell MCP clients when to use its tools instead of grep — server instructions plus a leading sentence on five tool descriptions — and measure the behavior change against the current Homebrew server.

**Architecture:** One module constant `SERVER_INSTRUCTIONS` in `src/jarvis/server.py`, passed to `FastMCP("jarvis", instructions=...)`. Five tool docstrings gain a new first sentence. Unit tests read the registered values through an in-memory MCP client session. A two-arm eval (Homebrew server vs. branch server, each the only MCP server in a headless Claude Code session) reuses text2prod's harness on jarvis-snapshot fixtures.

**Tech Stack:** Python 3.12, `mcp` SDK FastMCP, pytest + anyio (`create_connected_server_and_client_session`), `uv`; Bash + `jq` + `claude -p` for the eval; `jarvis` CLI (Homebrew 0.11.0 at `/opt/homebrew/bin/jarvis`).

**Spec:** `spec.md` (same folder). Intent: `intent.md`.

## Global Constraints

- Work only in the worktree `/Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance` on branch `feat/agent-tool-guidance`. Never edit, stage, or run `uv` in the main checkout `/Users/ddphuong/Projects/jarvis-ai/jarvis` (it has the user's uncommitted `AGENTS.md`). Never set `UV_PROJECT_ENVIRONMENT`.
- No new dependencies. Tool names, parameters, return shapes, and runtime behavior unchanged.
- `SERVER_INSTRUCTIONS[:512]` contains `getIndexStatus`, `findReferences`, and `grep`; `len(SERVER_INSTRUCTIONS) <= 2048`.
- Only these five tool docstrings change, and only by a new first sentence: `getIndexStatus`, `goToDefinition`, `findReferences`, `callHierarchy`, `typeHierarchy`. `documentSymbols`, `searchCode`, `semanticSearch`, `blastRadius`, `indexRepo` are unchanged.
- Commits: lowercase imperative Conventional Commits (`feat(server): …`, `test(server): …`, `docs(features): …`), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Unit gate green before each code commit: `uv run --frozen pytest -m "not integration" -rs` (run inside the worktree; it uses the worktree's own `.venv`).
- Eval: every session loads only one MCP server via `EVAL_MCP_CONFIG`; `run-eval.sh` exit 3 (`INVALID: jarvis not connected at init`) means restore and rerun (max 3 attempts). One Bash call per session: run → `git diff >file` → restore. Bash timeout 600000 ms; background if it runs longer.

---

### Task 1: Server instructions

**Files:**
- Modify: `src/jarvis/server.py:29` (the `mcp = FastMCP("jarvis")` line, plus a new constant just above it)
- Test: `tests/test_server_tools.py` (append)

**Interfaces:**
- Produces: `jarvis.server.SERVER_INSTRUCTIONS: str`; `jarvis.server.mcp.instructions == SERVER_INSTRUCTIONS`.

- [ ] **Step 1: Set up the worktree environment**

Run: `cd /Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance && uv sync --frozen -q && uv run --frozen pytest -q -m "not integration" tests/test_server_tools.py 2>&1 | tail -1`
Expected: all passed. Then confirm the main checkout is untouched: `cat /Users/ddphuong/Projects/jarvis-ai/jarvis/.venv/lib/python3.12/site-packages/__editable__.jarvis_mcp-0.11.0.pth` prints `/Users/ddphuong/Projects/jarvis-ai/jarvis/src`.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_server_tools.py`:

```python
def test_server_instructions_are_published():
    assert server.mcp.instructions == server.SERVER_INSTRUCTIONS


def test_server_instructions_lead_with_the_rule_and_fit_client_caps():
    head = server.SERVER_INSTRUCTIONS[:512]
    for needle in ("getIndexStatus", "findReferences", "grep"):
        assert needle in head
    assert len(server.SERVER_INSTRUCTIONS) <= 2048
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --frozen pytest -q tests/test_server_tools.py -k server_instructions`
Expected: FAIL with `AttributeError: module 'jarvis.server' has no attribute 'SERVER_INSTRUCTIONS'`.

- [ ] **Step 4: Add the constant and pass it**

In `src/jarvis/server.py`, replace:

```python
mcp = FastMCP("jarvis")
```

with:

```python
# Sent to every MCP client in the initialize result. The first paragraph is
# the whole rule and must fit in 512 characters (Codex reads that much as
# self-contained guidance); the total must stay under Claude Code's
# 2,048-character cap. tests/test_server_tools.py enforces both.
SERVER_INSTRUCTIONS = (
    "jarvis answers symbol questions from a precomputed index, more "
    "precisely than grep. In an indexed repo, for where X is defined, who "
    "uses or calls X, or what subclasses X: call getIndexStatus first, then "
    "goToDefinition, findReferences, callHierarchy, or typeHierarchy. "
    "`repo` is the index slug: by default the repo directory's name "
    "(lowercased, unsafe characters as `-`). Use grep or searchCode for "
    "strings, config keys, and prose. Not indexed or a tool error: fall "
    "back to grep and say so.\n\n"
    "Limits: findReferences, callHierarchy, and typeHierarchy need SCIP "
    "coverage; without it they return a capability error with recovery "
    "steps, not an empty answer. A stale index can miss recent edits: pass "
    "`repo_path` to getIndexStatus, and when it reports stale, confirm hits "
    "in changed files. searchCode searches git HEAD, so uncommitted edits "
    "are grep-only. documentSymbols outlines one file. blastRadius lists "
    "other indexed repos that depend on a package. semanticSearch is "
    "unavailable in the Homebrew build. indexRepo builds an index for a git "
    "repo path."
)

mcp = FastMCP("jarvis", instructions=SERVER_INSTRUCTIONS)
```

- [ ] **Step 5: Run the tests to verify they pass, then the unit gate**

Run: `uv run --frozen pytest -q tests/test_server_tools.py -k server_instructions` → 2 passed.
Run: `uv run --frozen python -c "from jarvis import server; s=server.SERVER_INSTRUCTIONS; print(len(s), s.index('Limits:'))"` → total ≤ 2048 and the `Limits:` offset ≤ 512 (paragraph 1 fits in the head).
Run: `uv run --frozen pytest -m "not integration" -rs 2>&1 | tail -3` → no failures.

- [ ] **Step 6: Commit**

```bash
git add src/jarvis/server.py tests/test_server_tools.py
git commit -m "feat(server): publish mcp instructions on when to prefer jarvis over grep" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Tool description lead sentences

**Files:**
- Modify: `src/jarvis/server.py` docstrings of `get_index_status` (~line 590), `go_to_definition` (~460), `find_references` (~482), `call_hierarchy` (~504), `type_hierarchy` (~526) — line numbers shift after Task 1; locate by `@mcp.tool(name="...")`.
- Test: `tests/test_server_tools.py` (append)

**Interfaces:**
- Consumes: `server.mcp` (Task 1), existing `EXPECTED_TOOLS` set in the test file.
- Produces: registered descriptions whose whitespace-normalized text starts with the sentences below.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server_tools.py`:

```python
LEAD_SENTENCES = {
    "getIndexStatus": (
        "Call once before the first symbol query in a repo: shows whether it "
        "is indexed, how fresh it is, and which tools are available."
    ),
    "goToDefinition": "Use instead of grep to find where a symbol is defined.",
    "findReferences": (
        "Use instead of grep to find every usage of a symbol, without matches "
        "in comments, docs, or look-alike names."
    ),
    "callHierarchy": "Use instead of grep to find what calls a function and what it calls.",
    "typeHierarchy": "Use instead of grep to find a type's supertypes and subtypes.",
}


@pytest.mark.anyio
async def test_symbol_tool_descriptions_lead_with_when_to_use():
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name, sentence in LEAD_SENTENCES.items():
        description = " ".join((tools[name].description or "").split())
        assert description.startswith(sentence), name
    for name in EXPECTED_TOOLS - LEAD_SENTENCES.keys():
        description = " ".join((tools[name].description or "").split())
        assert not description.startswith("Use instead of grep"), name
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --frozen pytest -q tests/test_server_tools.py -k lead_with_when_to_use`
Expected: FAIL with `AssertionError: getIndexStatus` (or the first tool checked).

- [ ] **Step 3: Edit the five docstrings**

Prepend the sentence and a blank line; keep the rest of each docstring byte-for-byte. Wrap at the file's existing width (about 76 columns). Exact results for the opening lines:

`get_index_status`:

```python
    """Call once before the first symbol query in a repo: shows whether it
    is indexed, how fresh it is, and which tools are available.

    Whether `repo` has a published index, and its freshness. Pass
```

`go_to_definition`:

```python
    """Use instead of grep to find where a symbol is defined.

    Resolve `symbol`'s definition location(s) within `repo`. `symbol`
```

`find_references`:

```python
    """Use instead of grep to find every usage of a symbol, without matches
    in comments, docs, or look-alike names.

    Every occurrence of `symbol` within `repo`, definition sites
```

`call_hierarchy`:

```python
    """Use instead of grep to find what calls a function and what it calls.

    Single-level incoming/outgoing call hierarchy for `symbol` within
```

`type_hierarchy`:

```python
    """Use instead of grep to find a type's supertypes and subtypes.

    Single-level super/subtypes for `symbol` within `repo`. `symbol` may
```

- [ ] **Step 4: Run the test, then the unit gate**

Run: `uv run --frozen pytest -q tests/test_server_tools.py -k lead_with_when_to_use` → 1 passed.
Run: `uv run --frozen pytest -m "not integration" -rs 2>&1 | tail -3` → no failures.
Run: `git diff --stat` → only `src/jarvis/server.py` and `tests/test_server_tools.py`.

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/server.py tests/test_server_tools.py
git commit -m "feat(server): lead symbol tool descriptions with when to use them" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Eval setup and baseline arm

**Files:**
- Create: `docs/features/agent-tool-guidance/eval-report.md`

**Interfaces:**
- Produces under `EVAL3=/private/tmp/claude-502/-Users-ddphuong-Projects-text2prod/3049c210-24d5-462f-a9e2-ae777a348f49/scratchpad/b1-eval`: `run-eval.sh`, `make-jarvis-fixtures.sh`, fixtures `b1-debug/`, `b1-review/`, `b1-stale/` (indexed as slugs `b1-debug`, `b1-review`, `b1-stale`), `t2p-main/` (text2prod `main` worktree), `mcp-baseline.json`, `mcp-branch.json`, `prompt-debug.txt`, `prompt-review.txt`, baseline artifacts `base-*.{jsonl,tools.txt,diff}`.

- [ ] **Step 1: Copy the harness and rename fixture dirs**

```bash
export EVAL3=/private/tmp/claude-502/-Users-ddphuong-Projects-text2prod/3049c210-24d5-462f-a9e2-ae777a348f49/scratchpad/b1-eval
T2P=/Users/ddphuong/Projects/text2prod
mkdir -p "$EVAL3"
git -C "$T2P" show feat/code-intel-aware-exploration:tests/code-intelligence/run-eval.sh >"$EVAL3/run-eval.sh"
git -C "$T2P" show feat/code-intel-aware-exploration:tests/code-intelligence/make-jarvis-fixtures.sh \
  | sed -e 's#${ROOT}/debug#${ROOT}/b1-debug#g' -e 's#${ROOT}/review#${ROOT}/b1-review#g' -e 's#${ROOT}/stale#${ROOT}/b1-stale#g' \
  >"$EVAL3/make-jarvis-fixtures.sh"
chmod +x "$EVAL3"/*.sh
git -C "$T2P" rev-parse --short feat/code-intel-aware-exploration
grep -c 'b1-' "$EVAL3/make-jarvis-fixtures.sh"
jarvis list | awk '{print $1}' | grep -xE 'b1-(debug|review|stale)' || echo "no slug collisions"
```

Expected: source commit printed (record it); `grep -c` ≥ 3; `no slug collisions`. If any slug exists, STOP and report BLOCKED.

- [ ] **Step 2: Build fixtures and fix the stale copy's environment**

```bash
"$EVAL3/make-jarvis-fixtures.sh" "$EVAL3" /Users/ddphuong/Projects/jarvis-ai/jarvis af5dbeb
rm -rf "$EVAL3/b1-stale/.venv" && (cd "$EVAL3/b1-stale" && uv sync --frozen -q)
(cd "$EVAL3/b1-debug" && uv run --frozen pytest -q -m "not integration" tests/test_query.py 2>&1 | tail -1)
```

Expected: exactly `1 failed` in `b1-debug`. (`b1-stale` was copied from `b1-review`; its `.venv` is rebuilt so its editable install points at itself.)

- [ ] **Step 3: Index under directory names, make stale, prompts, configs, plugin**

```bash
jarvis index "$EVAL3/b1-debug"; jarvis index "$EVAL3/b1-review"; jarvis index "$EVAL3/b1-stale"
jarvis status b1-debug; jarvis status b1-review; jarvis status b1-stale
cat >"$EVAL3/b1-stale/src/jarvis/reports.py" <<'PY'
from jarvis.dashboard import export_metrics_csv


def weekly_report(conn) -> str:
    return export_metrics_csv(conn, since="7d")
PY
git -C "$EVAL3/b1-stale" add src/jarvis/reports.py
git -C "$EVAL3/b1-stale" -c user.name=eval -c user.email=eval@example.invalid commit -qm "add weekly report"
cat >"$EVAL3/prompt-debug.txt" <<'TXT'
`uv run pytest -m "not integration" tests/test_query.py` fails on test_resolve_definition_bare_name_prefers_the_scip_covered_file_over_syntax. Find the cause and fix it.
TXT
cat >"$EVAL3/prompt-review.txt" <<'TXT'
Code review feedback from an external reviewer on this repo:

"export_metrics_csv in dashboard.py is a stub. Implement it properly: persist query metrics, add date-range filters, and stream large exports."

Address this feedback.
TXT
WT=/Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance
printf '{"mcpServers":{"jarvis":{"command":"/opt/homebrew/bin/jarvis-server"}}}\n' >"$EVAL3/mcp-baseline.json"
printf '{"mcpServers":{"jarvis":{"command":"uv","args":["run","--frozen","--directory","%s","jarvis-server"]}}}\n' "$WT" >"$EVAL3/mcp-branch.json"
git -C /Users/ddphuong/Projects/text2prod worktree add "$EVAL3/t2p-main" main
```

Expected: three `jarvis status` lines show indexed with SCIP available (else STOP, BLOCKED); `jarvis status b1-stale` after the commit shows the indexed commit differs from HEAD.

- [ ] **Step 4: Precheck the branch server reads these indexes**

```bash
(cd /Users/ddphuong/Projects/jarvis-ai/jarvis/.worktrees/agent-tool-guidance && \
  uv run --frozen python -c "from jarvis import server; r=server.get_index_status('b1-debug'); print(r.get('indexed'), r.get('error'))")
```

Expected: `True None`. Otherwise STOP and report BLOCKED with the output (index-format mismatch between the Homebrew CLI and the source build).

- [ ] **Step 5: Run the baseline arm (one Bash call per session)**

```bash
H="$EVAL3/run-eval.sh"; P="$EVAL3/t2p-main"; export EVAL_MCP_CONFIG="$EVAL3/mcp-baseline.json"
restore() { git -C "$1" reset -q --hard && git -C "$1" clean -qfd -e .venv; }
# for i in 1 2 3, each its own Bash call:
$H "$P" "$EVAL3/b1-debug" "$EVAL3/prompt-debug.txt" "$EVAL3/base-s1-$i"; git -C "$EVAL3/b1-debug" diff >"$EVAL3/base-s1-$i.diff"; restore "$EVAL3/b1-debug"
# then:
$H "$P" "$EVAL3/b1-review" "$EVAL3/prompt-review.txt" "$EVAL3/base-s2"; git -C "$EVAL3/b1-review" diff >"$EVAL3/base-s2.diff"; restore "$EVAL3/b1-review"
$H "$P" "$EVAL3/b1-stale" "$EVAL3/prompt-review.txt" "$EVAL3/base-s4"; git -C "$EVAL3/b1-stale" diff >"$EVAL3/base-s4.diff"; restore "$EVAL3/b1-stale"
```

For each session also record the server's instructions as seen at init, to prove the arms differ: `jq -r 'select(.type=="system" and .subtype=="init") | .mcp_servers[]? | select(.name=="jarvis")' <file>.jsonl`.

- [ ] **Step 6: Write the report's setup and baseline sections and commit**

Create `docs/features/agent-tool-guidance/eval-report.md`:

```markdown
# Eval report — agent-tool-guidance

Date: <date> · Harness: Claude Code <`claude --version`> headless · text2prod harness from `feat/code-intel-aware-exploration` @ <sha> · jarvis fixtures from `af5dbeb`

## Method

Two arms differing only in the MCP server, each the only server in the session (`--strict-mcp-config`), each run rejected unless jarvis is connected at init. Baseline: `/opt/homebrew/bin/jarvis-server` (0.11.0, no instructions). Branch: `uv run --directory <worktree> jarvis-server` at <branch sha>. Plugin in both arms: text2prod `main` worktree. Fixtures `b1-debug`, `b1-review`, `b1-stale` indexed under their directory names. S1 n=3, S2 n=1, S4 n=1 per arm.

## Baseline arm

| # | Run | jarvis at init | getIndexStatus | findReferences / callHierarchy | other jarvis | symbol greps | Outcome |
|---|---|---|---|---|---|---|---|
| S1 | 1 | | | | | | |
| S1 | 2 | | | | | | |
| S1 | 3 | | | | | | |
| S2 | 1 | | | | | | |
| S4 | 1 | | | | | | |
```

Fill every cell from `.tools.txt` and the final message / `.diff`. S1 outcome is correct if `query.py:545` sets `uncovered_only=True` or drops the argument. Commit:

```bash
git add docs/features/agent-tool-guidance/eval-report.md
git commit -m "docs(features): record agent-tool-guidance baseline eval" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Branch arm, verdict, cleanup

**Files:**
- Modify: `docs/features/agent-tool-guidance/eval-report.md`

**Interfaces:**
- Consumes: everything under `$EVAL3` from Task 3; `mcp-branch.json`.

- [ ] **Step 1: Confirm the branch server publishes the instructions**

Run one trivial session: `EVAL_MCP_CONFIG="$EVAL3/mcp-branch.json" "$EVAL3/run-eval.sh" "$EVAL3/t2p-main" "$EVAL3/b1-review" <(echo "Reply OK.") "$EVAL3/branch-probe"` → exit 0. If the stream exposes server instructions anywhere in the init event, record that; if not, note that the stream does not show them (the unit test in Task 1 is the evidence they are sent).

- [ ] **Step 2: Run the branch arm** — Task 3 Step 5's commands with `EVAL_MCP_CONFIG="$EVAL3/mcp-branch.json"` and `branch-` prefixes (S1 ×3, S2, S4), one Bash call per session.

- [ ] **Step 3: Append results and the verdict**

Append `## Branch arm` (same table) and:

```markdown
## Verdict

| Criterion (intent Decisions) | Result |
|---|---|
| Branch: getIndexStatus / findReferences / callHierarchy in ≥ 2 of 3 S1 runs | |
| Branch: same in the S2 run | |
| Baseline: none of those calls in any run | |
| All S1/S2 outcomes correct in both arms | |
| **Overall** | PASS / FAIL |

## Findings

- <one bullet per failed criterion, with evidence file>
- <S4 observation (not scored)>
```

A FAIL is recorded as a FAIL; do not change the instructions or descriptions to chase a pass.

- [ ] **Step 4: Clean up and commit**

```bash
jarvis forget b1-debug; jarvis forget b1-review; jarvis forget b1-stale
git -C /Users/ddphuong/Projects/text2prod worktree remove "$EVAL3/t2p-main"
cat /Users/ddphuong/Projects/jarvis-ai/jarvis/.venv/lib/python3.12/site-packages/__editable__.jarvis_mcp-0.11.0.pth
git add docs/features/agent-tool-guidance/eval-report.md
git commit -m "docs(features): record agent-tool-guidance branch eval and verdict" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected `.pth` line: `/Users/ddphuong/Projects/jarvis-ai/jarvis/src`.

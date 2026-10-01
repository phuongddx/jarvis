# Intent: Agents choose jarvis tools over grep for symbol questions

Author: PhuongDoan
Status: accepted <!-- draft | accepted | rejected -->
Date: 2026-09-28

## Problem

With jarvis connected and a repo fully indexed, coding agents still
answer symbol questions (where is X defined, who calls X, is X used)
with grep. Text2Prod's `code-intel-aware-exploration` evals measured it
on a 300-file jarvis snapshot: 14 sessions across baseline, a skill
pointer, and stronger skill wording made 0 jarvis tool calls, and
grepped 3–5 times each (text2prod
`docs/features/code-intel-aware-exploration/eval-report.md`). The
server gives agents no reason to switch. `src/jarvis/server.py:29`
creates `FastMCP("jarvis")` with no instructions, and tool docstrings
describe what each tool does, never when to use it instead of grep.
jarvis's core value ("your agent should not spend its context window on
grep") is not delivered by default.

## Proposed outcome

When jarvis is connected, the server itself tells the agent at session
start when to use its tools: symbol questions in an indexed repo, index
status first, grep for text. The tool descriptions reinforce this. In
the same eval harness, the modified server produces symbol-tool calls
where the current server produces none, with no loss of correct
outcomes. Every MCP client benefits, not only Claude Code.

## Affected users and systems

- Users: anyone running `jarvis-server` in an MCP client (Claude Code,
  Cursor, Codex, Claude Desktop).
- Systems / repos: `jarvis` — `src/jarvis/server.py` (server
  instructions, tool docstrings), `tests/test_server_tools.py`; eval
  harness reused from text2prod `tests/code-intelligence/`.

## Constraints

- No new dependencies; FastMCP already accepts `instructions=`.
- Tool names, parameters, and return shapes unchanged (other clients
  depend on them).
- Claude Code truncates server instructions and tool descriptions at
  2,048 characters; put the key rule first.
- Guidance must be honest about limits: SCIP-only tools
  (`findReferences`, `callHierarchy`, `typeHierarchy`) need SCIP
  coverage; `searchCode` indexes git HEAD, not uncommitted edits;
  `semanticSearch` is unavailable in the Homebrew build.
- jarvis conventions: Conventional Commits, `uv` only, unit gate green,
  MCP boundaries return structured errors.
- Scope: server-side text only. Plugin hooks (B3) and jarvis workflow
  policies (B2) are separate sub-projects.

## Decisions

- Success: in the round-2 harness (jarvis-only MCP, connected at
  init), the modified server gets `getIndexStatus`, `findReferences`, or
  `callHierarchy` calls in at least 2 of 3 S1 runs and in the S2 run;
  the current server gets none; every outcome stays correct.
- Placement: server instructions plus a leading sentence on the five
  symbol and status tools (approach A in `spec.md`).

## Open questions

None.

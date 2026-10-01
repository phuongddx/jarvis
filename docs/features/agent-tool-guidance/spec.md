# Spec: Agents choose jarvis tools over grep for symbol questions

Status: shipped <!-- draft | approved | shipped | superseded -->
Outcome: success criterion not met — instructions delivered verbatim, 0/5 sessions used jarvis symbol tools (see [eval-report.md](eval-report.md)). Shipped by owner decision: the text is accurate, has zero runtime cost, and reaches clients that read `instructions` and load full tool schemas. Changing Claude Code tool choice moves to B3 (jarvis-index hooks / schema loading).
Date: 2026-09-28
Intent: [intent.md](intent.md)

## Context read

- `AGENTS.md`, `CLAUDE.md`, `ARCHITECTURE.md`
- `docs/engineering/conventions.md` (Conventional Commits; MCP
  boundaries return structured errors; stdout machine-parseable; unit
  tests mock boundaries; `JARVIS_DATA_DIR` in state-touching tests)
- `docs/engineering/tech-stack.md`, `docs/engineering/infrastructure.md`
- `.agents/policies/` — empty
- `src/jarvis/server.py` (`FastMCP("jarvis")` at line 29, no
  instructions; tool docstrings at lines 416–800)
- `src/jarvis/config.py` (`repo_slug`: default slug is the directory
  name, lowercased, unsafe characters as `-`)
- `tests/test_server_tools.py`
- `jarvis-index/plugin/skills/jarvis-use/SKILL.md`,
  `jarvis-index/plugin/hooks/` (separate repo; B3)
- text2prod `docs/features/code-intel-aware-exploration/`
  (`eval-report.md`, `review.md`) and its shelved branch's
  `tests/code-intelligence/` harness
- Sibling specs: none (first feature)

Gaps:
- `.agents/policies/` and `.agents/skills/` are empty (B2).
- `AGENTS.md` has uncommitted local edits; not touched.

## References

- text2prod `code-intel-aware-exploration` eval report — 14 sessions,
  0 jarvis calls with jarvis connected and indexed; informed the problem
  statement and the eval harness.
- Claude Code changelog v2.1.281 (github.com/anthropics/claude-code,
  `CHANGELOG.md`) — 2,048-character cap on server instructions and tool
  descriptions; informed the length limit.
- Claude Code tool-search docs —
  https://code.claude.com/docs/en/agent-sdk/tool-search — server
  instructions and tool names load at session start and drive discovery;
  informed putting the rule in both instructions and descriptions.
- Codex MCP docs — https://learn.chatgpt.com/docs/extend/mcp?surface=cli
  — Codex reads `instructions`; "keep the first 512 characters
  self-contained"; informed the 512-character rule head.
- MCP lifecycle spec —
  https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
  — optional `InitializeResult.instructions`; informed the mechanism.
- FastMCP server docs — https://gofastmcp.com/servers/server —
  `instructions` parameter; informed the mechanism.
- Serena `mcp.py` —
  https://github.com/oraios/serena/blob/main/src/serena/mcp.py — passes
  its onboarding prompt as `instructions`; prior art.
- Serena issue #1201 — https://github.com/oraios/serena/issues/1201 —
  agents drift back to grep over long sessions; hooks were the fix;
  informed leaving drift to B3.
- Anthropic, advanced tool use —
  https://anthropic.com/engineering/advanced-tool-use — descriptive tool
  definitions improve discovery; informed the leading description
  sentence.

## Goals

1. The server tells every MCP client at session start when to use its
   tools: symbol questions in an indexed repo, index status first, grep
   for text, and how to derive the `repo` slug.
2. The five symbol and status tools say "use instead of grep" (or "call
   once first") in their first sentence.
3. In the eval, the modified server produces symbol-tool calls where
   the current server produces none, with no loss of correct outcomes.

## Non-goals

- Changing tool names, parameters, return shapes, or behavior.
- Plugin hooks or `jarvis-use` edits (B3); workflow policies (B2).
- Editing the other five tool descriptions or the README tools table.
- Onboarding tools or prompts (Serena-style `initial_instructions`).
- Countering drift over long sessions (B3).

## Design

### Server instructions

`src/jarvis/server.py` gets one module constant, `SERVER_INSTRUCTIONS`,
passed as `FastMCP("jarvis", instructions=SERVER_INSTRUCTIONS)`. It is
two paragraphs.

Paragraph 1 (the rule; must fit in the first 512 characters):

> jarvis answers symbol questions from a precomputed index, more
> precisely than grep. In an indexed repo, for where X is defined, who
> uses or calls X, or what subclasses X: call getIndexStatus first, then
> goToDefinition, findReferences, callHierarchy, or typeHierarchy.
> `repo` is the index slug: by default the repo directory's name
> (lowercased, unsafe characters as `-`). Use grep or searchCode for
> strings, config keys, and prose. Not indexed or a tool error: fall
> back to grep and say so.

Paragraph 2 (limits):

> Limits: findReferences, callHierarchy, and typeHierarchy need SCIP
> coverage; without it they return a capability error with recovery
> steps, not an empty answer. A stale index can miss recent edits: pass
> `repo_path` to getIndexStatus, and when it reports stale, confirm hits
> in changed files. searchCode searches git HEAD, so uncommitted edits
> are grep-only. documentSymbols outlines one file. blastRadius lists
> other indexed repos that depend on a package. semanticSearch is
> unavailable in the Homebrew build. indexRepo builds an index for a git
> repo path.

Total length at most 2,048 characters (about 1,020 expected).

### Tool descriptions

Each of these docstrings gets one new first sentence. The rest of the
docstring is unchanged.

| Tool | New first sentence |
|---|---|
| `getIndexStatus` | Call once before the first symbol query in a repo: shows whether it is indexed, how fresh it is, and which tools are available. |
| `goToDefinition` | Use instead of grep to find where a symbol is defined. |
| `findReferences` | Use instead of grep to find every usage of a symbol, without matches in comments, docs, or look-alike names. |
| `callHierarchy` | Use instead of grep to find what calls a function and what it calls. |
| `typeHierarchy` | Use instead of grep to find a type's supertypes and subtypes. |

`documentSymbols`, `searchCode`, `semanticSearch`, `blastRadius`, and
`indexRepo` are unchanged.

## Error handling

No runtime behavior changes. Tool error payloads, capability errors,
and recovery guidance are untouched. A client that ignores
`instructions` sees only the new description sentences.

## Testing

**Unit tests** (`tests/test_server_tools.py`):
- `mcp.instructions == SERVER_INSTRUCTIONS`.
- `SERVER_INSTRUCTIONS[:512]` contains `getIndexStatus`,
  `findReferences`, and `grep`; `len(SERVER_INSTRUCTIONS) <= 2048`.
- The five edited tools' registered descriptions start with their new
  sentences (read from the registered tool list, not source text); the
  other five tools' descriptions do not start with "Use instead of
  grep".
- Unit gate green: `uv run pytest -m "not integration" -rs`.

**Behavior eval** (results in `eval-report.md` in this folder):
- Harness: text2prod's `tests/code-intelligence/run-eval.sh` and
  `make-jarvis-fixtures.sh`, copied to scratch from the shelved branch
  `feat/code-intel-aware-exploration` with `git show`; the source
  commit is recorded in the report.
- Fixtures: jarvis `af5dbeb` snapshots with A's round-2 seeds (S1:
  `query.py:545` `uncovered_only=False`; S2: unused
  `export_metrics_csv`; S4: S2 plus a caller committed after indexing).
  Each is indexed under its directory name (`b1-debug`, `b1-review`,
  `b1-stale`), so the slug rule resolves. Those slugs must not already
  exist in `jarvis list`.
- Two arms differing only in the MCP server, each session loading only
  that server and rejected unless jarvis is connected at init:
  baseline `/opt/homebrew/bin/jarvis-server`; branch
  `uv run --directory <feature worktree> jarvis-server` using the
  worktree's own `.venv`. The plugin in both arms is text2prod `main`
  (no code-intel pointers).
- Runs: S1 ×3, S2, S4 per arm (10 sessions).
- Pass: branch arm calls `getIndexStatus`, `findReferences`, or
  `callHierarchy` in at least 2 of 3 S1 runs and in the S2 run; the
  baseline arm makes none; every outcome is correct (S1 fix at the
  `query.py:545` call site; S2 reports unused and proposes removal).
  S4 is recorded, not scored.
- Precheck before any session: one branch-server `getIndexStatus`
  call on `b1-debug` must return `indexed: true` (guards against an
  index-format mismatch between the Homebrew CLI and the source build).
- Never write to the main jarvis checkout's `.venv`; never set
  `UV_PROJECT_ENVIRONMENT`.

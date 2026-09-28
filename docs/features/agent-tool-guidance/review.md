# Review: agent-tool-guidance

Reviewer: per-task subagent reviewers plus a final whole-branch reviewer; recorded by the controller
Date: 2026-09-28
Plan under review: `plan.md` in this directory

## Outcome

- Code: `SERVER_INSTRUCTIONS` published via `FastMCP(instructions=…)`,
  plus a lead sentence on five tool descriptions. Unit gate at
  `b63ad59`: 900 passed, 32 skipped (semantic tests need `lancedb`),
  20 deselected.
- Eval ([`eval-report.md`](eval-report.md), evidence in
  `eval-evidence/`): **FAIL** against the intent's success bar. The
  branch server's instructions reached the model verbatim (delivery
  check), yet 0 of 5 branch sessions called `getIndexStatus`,
  `findReferences`, or `callHierarchy`. The baseline also made 0.
  Outcomes were correct in both arms.
- The description edits were not tested: Claude Code defers MCP tool
  schemas behind ToolSearch, so the model saw only tool names.

## Passes

Each task got one review covering spec compliance and quality, which
included the bugs pass. A final whole-branch review looked at
correctness, how accurate the instructions are against the source,
test quality, conventions, and whether the eval's conclusions hold.
No security surface: the diff is a string constant, docstrings, tests,
and docs.

## Findings

| # | Severity | Pass | Location | Note |
| --- | --- | --- | --- | --- |
| 1 | Important (fixed) | Compliance | `eval-report.md` | Cited ephemeral scratch evidence, and made a false cleanup claim. Evidence now committed in `eval-evidence/`. |
| 2 | Important (fixed) | Compliance | `eval-report.md` | Internal process text removed. |
| 3 | Important (fixed) | Compliance | `eval-report.md` | Overstated what the unit test proves; wire delivery is now credited to the probe. |
| 4 | Important (open, owner decision) | Compliance | `spec.md`, `intent.md` | The success criterion failed; merging needs an explicit recorded decision. |
| 5 | Nit (fixed) | Bugs | `tests/test_server_tools.py` | Added a paragraph-break ≤ 512 assertion; the published-instructions test now reads the SDK's initialization options. |
| 6 | Nit (fixed) | Compliance | `intent.md` | Affected-systems list corrected. |
| 7 | Nit (parked) | Bugs | `src/jarvis/server.py` instructions | "searchCode searches git HEAD" really means the indexed commit. Changing it would change approved shipped wording. |
| 8 | Nit (fixed) | Compliance | `eval-report.md` | Method now shows `--frozen`. |

## Verdict

The final reviewer's verdict: ready to merge with fixes, and those
fixes are applied. The code is correct, accurate, and in scope. Merging
against a failed success criterion (finding 4) is the owner's decision.

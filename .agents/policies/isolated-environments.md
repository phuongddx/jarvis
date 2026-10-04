# Policy: scratch work never touches a real environment

**Rule.** Every scratch clone, eval fixture, and worktree gets its own
`.venv`. Create it with `uv sync --frozen` run inside that directory.

- Never set `UV_PROJECT_ENVIRONMENT` to another checkout's `.venv`. uv
  re-syncs the project and repoints that checkout's editable install at
  the scratch copy.
- Eval fixtures are indexed under their directory name (`jarvis index
  <dir>`, no `--slug`), so the default slug rule an agent is told about
  resolves. Remove them with `jarvis forget <slug>` during cleanup.
- Automation never edits `~/.claude/` or `~/.claude.json`, and that
  includes trust flags for scratch directories. If a headless session
  needs permissions, pass them per session (`--settings`,
  `--permission-mode`), or stop and ask.

**Why.** On 2026-09-28, an experiment ran `uv run` in a scratch clone
with `UV_PROJECT_ENVIRONMENT` pointing at this checkout's `.venv`. That
silently repointed its editable install at the clone. Later, an eval
agent marked a scratch directory as trusted in `~/.claude.json` to make
its permissions apply.

**Check.**
`.venv/lib/python3.12/site-packages/__editable__.jarvis_mcp-*.pth` in
the main checkout prints that checkout's own `src`. After an eval,
`jarvis list` shows no eval slugs.

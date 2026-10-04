# Policy: branch from origin/main; a PR carries only its own commits

**Rule.** Start every branch from the remote tip:

```sh
git fetch origin
git worktree add .worktrees/<topic> -b <type>/<topic> origin/main
```

- Never branch from local `main`. It can hold commits that were never
  pushed.
- Worktrees live under `.worktrees/`, which is gitignored. Remove them
  once the branch merges.
- Before opening or merging a PR, `git log --oneline origin/main..HEAD`
  must list only that PR's commits: one concern per PR, as in
  `docs/engineering/conventions.md`.
- Merge only a head that CI passed on
  (`gh pr merge --match-head-commit <sha>`).

**Why.** In the `agent-tool-guidance` and src-layout fix work, both
branches were cut from a local `main` holding 4 unpushed chore commits.
Both PRs silently carried them and had to be rebased before merging.

**Check.** `git merge-base HEAD origin/main` equals `origin/main` at
branch time, and the PR's commit list matches the work it describes.

# Semantic Install Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `jarvis install-semantic` so a source-checkout user can prepare semantic search without typing the uv command manually.

**Architecture:** The new source-only CLI subcommand locates the jarvis checkout, runs the canonical uv extras sync as one fixed argv, invalidates Python import caches, and verifies that semantic imports are available. It refuses clearly in the frozen Homebrew distribution, which cannot receive semantic dependencies after packaging.

**Tech Stack:** Python 3.12, argparse, uv, pytest.

**Spec:** [`docs/superpowers/specs/2026-07-29-semantic-search-design.md`](../specs/2026-07-29-semantic-search-design.md) § Dependencies, plus the 2026-09-15 user request for a dedicated install command.

## Assumptions

- “Instead of using uv” means the user should not have to remember and type the uv extras command; uv remains the locked implementation tool, matching the repository runtime preference.
- The command installs support only. Indexing remains an explicit `jarvis index ... --semantic` decision because embedding can download model weights.
- The Homebrew standalone build remains semantic-free. A post-build package manager cannot add dependencies to its frozen executable.

## Global Constraints

- The user-facing command is exactly `jarvis install-semantic`; it takes no arguments.
- In a source checkout, run exactly this argv from the checkout root: `["<resolved-uv>", "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"]`.
- Never build the command with a shell string.
- Bound the subprocess at `timeout=600`.
- Success stdout is exactly `semantic support installed`; diagnostics go to stderr.
- Exit `0` on success or when support is already installed; exit `1` on frozen build, missing checkout, missing uv, subprocess failure, timeout, or unavailable imports after sync.
- Do not add dependencies, change extras, bump versions, or change `uv.lock`.
- Do not attempt to install semantic dependencies into the Homebrew frozen build.
- Preserve the existing interactive post-index offer and all `_install_semantic_extra` behavior.
- Conventional Commits use lowercase imperative subjects.

---

### Task 1: Add the source-only install command

**Files:**
- Modify: `src/jarvis/index_cli.py:1-5`
- Modify: `src/jarvis/index_cli.py:770-795`
- Modify: `src/jarvis/index_cli.py:2077-2125`
- Test: `tests/test_index_cli.py:4325-4395`

**Interfaces:**
- Consumes: `runtime.is_frozen() -> bool`, `_semantic_extra_missing() -> bool`, `shutil.which`, `subprocess.run`.
- Produces: `_semantic_project_root() -> Path` and `_cmd_install_semantic(args: argparse.Namespace) -> int`; `build_parser()` maps `install-semantic` to that handler.

- [ ] **Step 1: Create the feature branch**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c feat/semantic-install-command
```

Expected: branch starts at `origin/main`; only pre-existing unrelated untracked files remain.

- [ ] **Step 2: Write the failing parser and behavior tests**

Append these tests to `tests/test_index_cli.py` near the existing semantic-flag tests:

```python
def test_install_semantic_command_parses_without_arguments():
    from jarvis import index_cli

    parser = index_cli.build_parser()
    args = parser.parse_args(["install-semantic"])

    assert args.func is index_cli._cmd_install_semantic


def test_install_semantic_refuses_frozen_build(monkeypatch, capsys):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: True)

    def forbidden():
        raise AssertionError("a frozen build must never install semantic support")

    monkeypatch.setattr(index_cli, "_semantic_extra_missing", forbidden)
    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "Homebrew binary distribution" in captured.err


def test_install_semantic_reports_already_installed(monkeypatch, capsys):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: False)

    def forbidden():
        raise AssertionError("installed support must not trigger another sync")

    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == "semantic support already installed\n"
    assert captured.err == ""


def test_install_semantic_reports_missing_source_checkout(monkeypatch, capsys, tmp_path):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: tmp_path)

    def forbidden():
        raise AssertionError("a non-checkout must never invoke uv")

    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "source checkout" in captured.err


def test_install_semantic_reports_missing_uv(monkeypatch, capsys, tmp_path):
    import argparse

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: None)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "requires uv" in captured.err


def test_install_semantic_runs_locked_sync(monkeypatch, capsys, tmp_path):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    uv_path = "/fake/uv"
    calls: list[tuple[list[str], Path, int]] = []
    invalidations: list[int] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append((cmd, cwd, timeout))
        return subprocess_module.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: uv_path)
    monkeypatch.setattr(index_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: False)
    monkeypatch.setattr("importlib.invalidate_caches", lambda: invalidations.append(1))

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert calls == [
        (
            [uv_path, "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"],
            root,
            600,
        )
    ]
    assert invalidations == [1]
    assert captured.out == "semantic support installed\n"
    assert captured.err == "installing semantic support...\n"


def test_install_semantic_reports_sync_failure(monkeypatch, capsys, tmp_path):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")

    def fake_run(cmd, *, cwd, timeout):
        return subprocess_module.CompletedProcess(
            cmd, 1, stdout="sync failed", stderr="network unavailable"
        )

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(index_cli.subprocess, "run", fake_run)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "semantic support installation failed" in captured.err
    assert "network unavailable" in captured.err


def test_install_semantic_reports_imports_unavailable_after_sync(
    monkeypatch, capsys, tmp_path
):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(
        index_cli.subprocess,
        "run",
        lambda cmd, *, cwd, timeout: subprocess_module.CompletedProcess(
            cmd, 0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: True)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "lancedb" in captured.err
    assert "sentence_transformers" in captured.err
```

- [ ] **Step 3: Run the new tests and verify they fail**

```bash
uv run pytest tests/test_index_cli.py -k "install_semantic_command or install_semantic_reports or install_semantic_runs" -q
```

Expected: FAIL because `_cmd_install_semantic` and `_semantic_project_root` do not exist.

- [ ] **Step 4: Implement the command**

In `src/jarvis/index_cli.py`, update the module docstring’s subcommand list to include `install-semantic`.

Immediately after `_install_semantic_extra`, add:

```python
def _semantic_project_root() -> Path:
    """Return the checkout that owns this source-build CLI."""
    return Path(__file__).resolve().parents[2]


def _cmd_install_semantic(args: argparse.Namespace) -> int:
    """Prepare semantic dependencies in a source checkout without shell work."""
    del args
    if runtime.is_frozen():
        print(
            "semantic support is not installable — the Homebrew binary "
            "distribution excludes semantic dependencies",
            file=sys.stderr,
        )
        return 1

    project_root = _semantic_project_root()
    if not (project_root / "pyproject.toml").is_file():
        print(
            "semantic support installation requires a jarvis source checkout "
            f"(missing {project_root / 'pyproject.toml'})",
            file=sys.stderr,
        )
        return 1

    uv = shutil.which("uv")
    if uv is None:
        print("semantic support installation requires uv", file=sys.stderr)
        return 1

    print("installing semantic support...", file=sys.stderr)
    try:
        result = subprocess.run(
            [uv, "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"],
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        print("semantic support installation failed", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print("semantic support installation failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return 1

    importlib.invalidate_caches()
    if _semantic_extra_missing():
        print(
            "semantic support installation completed but lancedb or "
            "sentence_transformers is unavailable",
            file=sys.stderr,
        )
        return 1

    print("semantic support installed")
    return 0
```

Add `import importlib` beside the existing `importlib.util` import.

In `build_parser()`, after the `reindex_parser` setup and before `forget_parser`, add:

```python
    install_semantic_parser = subparsers.add_parser(
        "install-semantic",
        help="install source-build semantic dependencies",
    )
    install_semantic_parser.set_defaults(func=_cmd_install_semantic)
```

- [ ] **Step 5: Run focused tests**

```bash
uv run pytest tests/test_index_cli.py -k "install_semantic_command or install_semantic_reports or install_semantic_runs" -q
```

Expected: all new tests PASS.

- [ ] **Step 6: Run the surrounding semantic and CLI tests**

```bash
uv run pytest tests/test_index_cli.py -q
```

Expected: all tests PASS with no regressions.

- [ ] **Step 7: Commit the command**

```bash
git add src/jarvis/index_cli.py tests/test_index_cli.py
git commit -m "feat(cli): add semantic install command"
```

Expected: only the implementation and test files are committed.

---

### Task 2: Document the source-only command

**Files:**
- Modify: `README.md:113-126`
- Modify: `docs/code-standards.md:358-372`
- Modify: `docs/code-standards.md:436-450`

**Interfaces:**
- Consumes: `jarvis install-semantic` from Task 1.
- Produces: user-facing source-build workflow and canonical CLI-surface documentation.

- [ ] **Step 1: Add the source workflow to README**

After the existing `<details><summary>Not included</summary>` block, add a sibling block:

````markdown
<details>
<summary>Source-build semantic search</summary>

The standalone Homebrew package excludes semantic dependencies. In a source
checkout, install them with:

```bash
uv run jarvis install-semantic
```

Then index explicitly:

```bash
uv run jarvis index /path/to/repo --semantic
```

The first embedding run downloads the configured model.
</details>
````

- [ ] **Step 2: Update both CLI-surface blocks in the standards doc**

In both “CLI Design → Command Structure” code blocks, add this line after `jarvis reindex`:

```bash
jarvis install-semantic
```

In the first block, add this sentence immediately after the command block:

```markdown
`jarvis install-semantic` is source-only; it runs the canonical uv extras sync and refuses in the frozen Homebrew distribution.
```

Do not describe it as available in the standalone package.

- [ ] **Step 3: Verify documentation statements**

```bash
grep -n 'uv run jarvis install-semantic' README.md
grep -n 'source-only' docs/code-standards.md
test "$(grep -c 'jarvis install-semantic' docs/code-standards.md)" -ge 3
```

Expected: all commands exit 0 and the README shows the source-only workflow.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md docs/code-standards.md
git commit -m "docs(cli): document semantic install command"
```

Expected: only the two documentation files are committed.

---

### Task 3: Validate, open the PR, and merge only when CI is green

**Files:**
- No additional source files changed.

**Interfaces:**
- Consumes: Task 1 implementation and Task 2 documentation commits.
- Produces: merged `origin/main` containing `jarvis install-semantic`.

- [ ] **Step 1: Verify clean worktree and intended commits**

```bash
git status --short
git log --oneline --reverse origin/main..HEAD
```

Expected: clean tracked worktree and exactly the implementation and documentation commits.

- [ ] **Step 2: Run the complete local gate**

```bash
uv sync --extra semantic --extra watch --group native
uv run pytest -m "not integration" -rs
uv run python scripts/check_versions.py
```

Expected: pytest exits 0 and the version checker prints `versions consistent`.

- [ ] **Step 3: Exercise the command in the already-provisioned checkout**

```bash
uv run jarvis install-semantic
uv run jarvis install-semantic
```

Expected: the first command exits 0 and prints `semantic support installed`; the second exits 0 and prints `semantic support already installed`. Diagnostics may appear on stderr from uv on the first run.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feat/semantic-install-command
cat > /tmp/jarvis-semantic-install-pr.md <<'PR_BODY'
## Summary
- add `jarvis install-semantic` for source checkouts
- run the canonical semantic/watch extras sync with a fixed argv
- refuse clearly in the frozen Homebrew distribution

## Validation
- `uv run pytest -m "not integration" -rs`
- `uv run python scripts/check_versions.py`
- `uv run jarvis install-semantic`
PR_BODY
gh pr create \
  --repo phuongddx/jarvis \
  --base main \
  --head feat/semantic-install-command \
  --title "feat(cli): add semantic install command" \
  --body-file /tmp/jarvis-semantic-install-pr.md
```

Expected: GitHub prints a pull-request URL.

- [ ] **Step 5: Wait for green CI, then merge**

```bash
PR_NUMBER="$(gh pr view feat/semantic-install-command \
  --repo phuongddx/jarvis --json number --jq .number)"
gh pr checks "$PR_NUMBER" --repo phuongddx/jarvis --watch --fail-fast --interval 30
gh pr view "$PR_NUMBER" --repo phuongddx/jarvis --json state,mergeable \
  --jq '.state == "OPEN" and .mergeable == "MERGEABLE"'
gh pr merge "$PR_NUMBER" --repo phuongddx/jarvis --merge
```

Expected: all checks pass, mergeability is `MERGEABLE`, and the PR merges without force.

---

## Failure Handling

- If `uv sync` succeeds but imports remain unavailable, stop with exit 1; do not mark semantic support installed.
- If `uv` is missing, report it and preserve the repository rule that uv is the supported installer.
- If the process is frozen, refuse without invoking uv; do not attempt to mutate the Homebrew bundle.
- If CI fails, fix on the feature branch and re-run the complete gate; do not force merge.

## Verification Summary

- `jarvis install-semantic` exists, takes no arguments, and is dispatched by `build_parser()`.
- Source checkout success runs `uv sync --extra semantic --extra watch --group native` with a 600-second timeout.
- Frozen Homebrew builds, missing checkouts, missing uv, failed syncs, and post-sync import failures exit 1 with stderr diagnostics.
- Success or already-installed states exit 0 with machine-parseable stdout.
- README and both standards-doc CLI surfaces describe the command as source-only.
- Full unit gate and version checker pass; CI is green before merge.
